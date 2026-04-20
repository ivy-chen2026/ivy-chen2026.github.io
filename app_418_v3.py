from flask import Flask, render_template_string, request, redirect, flash, session, get_flashed_messages, jsonify
import sqlite3
import hashlib
import re
import threading
import requests
from curl_cffi import requests as curl_requests
import csv
from datetime import datetime
from io import StringIO, BytesIO
import time
import pdfplumber
import json
import os



app = Flask(__name__)
app.secret_key = "sanction_list_flask_2026"
app.config['PERMANENT_SESSION_LIFETIME'] = 3600 * 24

# ====================== 缓存配置 ======================
CACHE_DIR = "data_cache"
US_CACHE = os.path.join(CACHE_DIR, "us.json")
JP_CACHE = os.path.join(CACHE_DIR, "jp.json")
EU_CACHE = os.path.join(CACHE_DIR, "eu.json")

if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)

def save_cache(data, path):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except:
        pass

def load_cache(path):
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except:
        pass
    return None

# ====================== 工具函数 ======================
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def standard_name(s):
    if not s:
        return ""
    s = str(s).upper().strip()
    s = re.sub(r"[^\w\s\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s

def get_user():
    if "uid" in session:
        try:
            conn = sqlite3.connect("sanction.db")
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM users WHERE id=?", (session["uid"],))
            u = c.fetchone()
            conn.close()
            return u
        except:
            return None
    return None

def get_user_favorite_ids(user_id):
    if not user_id:
        return []
    try:
        conn = sqlite3.connect("sanction.db")
        c = conn.cursor()
        c.execute("SELECT entity_id FROM favorites WHERE user_id=?", (user_id,))
        res = [x[0] for x in c.fetchall()]
        conn.close()
        return res
    except:
        return []

# ====================== 数据库初始化（永久保存收藏） ======================
def init_database():
    conn = sqlite3.connect("sanction.db")
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS entity_list (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        alt_names TEXT,
        source TEXT,
        list_type TEXT,
        address TEXT,
        country TEXT,
        data_source TEXT NOT NULL,
        UNIQUE(name, alt_names, data_source)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        keyword TEXT NOT NULL,
        result INTEGER DEFAULT 0,
        create_time TEXT
    )''')

    # ✅ 收藏表：永久保存，不会删除
    c.execute('''CREATE TABLE IF NOT EXISTS favorites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        entity_id INTEGER NOT NULL,
        create_time TEXT,
        UNIQUE(user_id, entity_id)
    )''')

    c.execute("SELECT * FROM users WHERE email='admin@test.com'")
    if not c.fetchone():
        pwd = hashlib.md5("admin123".encode()).hexdigest()
        c.execute("INSERT INTO users VALUES (1,'admin','admin@test.com',?,1)", (pwd,))

    conn.commit()
    conn.close()

# ====================== 【关键修复】不清空表，只插入不存在的数据 ======================
def insert_data_if_not_exists(data_list):
    try:
        conn = sqlite3.connect("sanction.db")
        c = conn.cursor()
        for item in data_list:
            c.execute('''
                INSERT OR IGNORE INTO entity_list
                (name, alt_names, source, list_type, address, country, data_source)
                VALUES (?,?,?,?,?,?,?)
            ''', (
                item["name"], item["alt_names"], item["source"],
                item["list_type"], item["address"], item["country"], item["data_source"]
            ))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print("入库失败:", e)
        return False

# ====================== 美国 CSL ======================
def load_us(force=False):
    if not force:
        data = load_cache(US_CACHE)
        if data is not None:
            print("✅ 美国数据从缓存加载")
            insert_data_if_not_exists(data)
            return True

    print("🔄 重新爬取美国数据")
    try:
        url = "https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.csv"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=120)
        resp.raise_for_status()
        reader = csv.DictReader(StringIO(resp.text))
        data = []
        for row in reader:
            data.append({
                "name": row.get("name", "").strip(),
                "alt_names": row.get("alternate_names", "").strip(),
                "source": row.get("source", "US Department of Commerce") or "US Department of Commerce",
                "list_type": row.get("type", "Entity List").strip(),
                "address": row.get("street_address", "").strip(),
                "country": row.get("country", "").strip(),
                "data_source": "US"
            })
        save_cache(data, US_CACHE)
        insert_data_if_not_exists(data)
        return True
    except Exception as e:
        print("美国爬取失败:", e)
        return False

# ====================== 日本 EUL ======================
def load_jp(force=False):
    if not force:
        data = load_cache(JP_CACHE)
        if data is not None:
            print("✅ 日本数据从缓存加载")
            insert_data_if_not_exists(data)
            return True

    print("🔄 重新爬取日本数据")
    try:
        pdf_entries = [
            {"url": "https://www.meti.go.jp/press/2025/09/20250929006/20250929006-1.pdf"},
            {"url": "https://www.meti.go.jp/press/2024/01/20250131003/20250131003-1.pdf"}
        ]
        pdf_content = None
        for entry in pdf_entries:
            try:
                resp = curl_requests.get(entry["url"], impersonate="chrome120", timeout=60)
                if resp.status_code == 200 and "%PDF" in resp.content[:10].decode('latin1'):
                    pdf_content = resp.content
                    break
            except:
                continue
        if not pdf_content:
            raise Exception("PDF获取失败")

        data = []
        with pdfplumber.open(BytesIO(pdf_content)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                for t in tables:
                    for r in t:
                        if not r or len(r) < 3: continue
                        if not str(r[0] or "").strip().isdigit(): continue
                        name = str(r[2] or "").strip().replace("\n", " ")
                        alias = str(r[3] or "").strip().replace("\n", " / ")
                        if name:
                            data.append({
                                "name": name,
                                "alt_names": alias,
                                "source": "Japan METI",
                                "list_type": "End User List",
                                "address": "",
                                "country": "",
                                "data_source": "JP"
                            })
        save_cache(data, JP_CACHE)
        insert_data_if_not_exists(data)
        return True
    except Exception as e:
        print("日本爬取失败:", e)
        return False

# ====================== 欧盟 FSF ======================
def load_eu(force=False):
    if not force:
        data = load_cache(EU_CACHE)
        if data is not None:
            print("✅ 欧盟数据从缓存加载")
            insert_data_if_not_exists(data)
            return True

    print("🔄 重新爬取欧盟数据")
    try:
        url = "https://data.opensanctions.org/datasets/latest/eu_fsf/targets.simple.csv"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=120)
        resp.raise_for_status()
        reader = csv.DictReader(StringIO(resp.text))
        data = []
        for row in reader:
            data.append({
                "name": row.get("name", "").strip(),
                "alt_names": row.get("aliases", "").strip(),
                "source": "EU FSF Sanctions",
                "list_type": "EU Sanction List",
                "address": row.get("addresses", "")[:300],
                "country": row.get("countries", "")[:50],
                "data_source": "EU"
            })
        save_cache(data, EU_CACHE)
        insert_data_if_not_exists(data)
        return True
    except Exception as e:
        print("欧盟爬取失败:", e)
        return False

# ====================== 加载数据（不删表） ======================
def load_all_data(force=False):
    load_us(force)
    load_jp(force)
    load_eu(force)
    print("🚀 数据加载完成（收藏数据永久保留）")

# ====================== 收藏异步接口（永久保存） ======================
@app.route("/api/favorite/<int:entity_id>", methods=["POST"])
def api_favorite(entity_id):
    user = get_user()
    if not user:
        return jsonify({"ok": False, "msg": "请先登录"})

    try:
        conn = sqlite3.connect("sanction.db")
        c = conn.cursor()
        c.execute("INSERT INTO favorites (user_id, entity_id, create_time) VALUES (?,?,?)",
                  (user["id"], entity_id, now_str()))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "action": "add", "msg": "收藏成功"})
    except sqlite3.IntegrityError:
        conn = sqlite3.connect("sanction.db")
        c = conn.cursor()
        c.execute("DELETE FROM favorites WHERE user_id=? AND entity_id=?",
                  (user["id"], entity_id))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "action": "remove", "msg": "已取消收藏"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ====================== 收藏页面 ======================
@app.route("/favorites")
def favorites_page():
    user = get_user()
    if not user:
        flash("请先登录", "warning")
        return redirect("/login")

    conn = sqlite3.connect("sanction.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''SELECT e.* FROM entity_list e
                 JOIN favorites f ON e.id = f.entity_id
                 WHERE f.user_id=?
                 ORDER BY f.id DESC''', (user["id"],))
    rows = c.fetchall()
    conn.close()

    result_html = ""
    if rows:
        for r in rows:
            alias_html = f'<p><strong>别名：</strong>{r["alt_names"]}</p>' if r["alt_names"] else ""
            addr_html = f'<p><strong>地址：</strong>{r["address"]}</p>' if r["address"] else ''
            country_html = f'<span class="badge bg-secondary me-2">{r["country"]}</span>' if r["country"] else ''
            source_label = {"US":"美国CSL","JP":"日本EUL","EU":"欧盟FSF"}.get(r["data_source"],"未知")
            source_badge = f'<span class="badge badge-{r["data_source"].lower()} me-2">{source_label}</span>'

            fav_btn = f'''<button class="btn btn-sm btn-danger fav-btn" data-id="{r['id']}">
                            <i class="fa fa-star"></i> 已收藏
                          </button>'''

            result_html += f'''
            <div class="card card-custom mb-3">
                <div class="card-body p-4">
                    <div class="d-flex justify-content-between">
                        <h5 class="card-title mb-2">{r["name"]}</h5>
                        {fav_btn}
                    </div>
                    {alias_html}
                    <div class="d-flex flex-wrap gap-2 mb-2">
                        {source_badge} {country_html}
                        <span class="badge bg-info">{r["list_type"]}</span>
                        <span class="badge bg-dark">{r["source"]}</span>
                    </div>
                    {addr_html}
                </div>
            </div>
            '''
    else:
        result_html = '''<div class="alert text-center">暂无收藏</div>'''

    content = f'''
    <div class="row justify-content-center">
        <div class="col-md-10 col-lg-8">
            <div class="card card-custom">
                <div class="card-header-custom">我的收藏</div>
                <div class="card-body-custom">
                    {result_html}
                    <a href="/" class="btn btn-outline-primary">返回</a>
                </div>
            </div>
        </div>
    </div>
    '''
    return render_template_string(base_template(content, user))

# ====================== 页面模板（完全不变） ======================
def base_template(content, user=None):
    return f'''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>国际实体清单查询系统（美+日+欧）</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/font-awesome@4.7.0/css/font-awesome.min.css" rel="stylesheet">
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: "Microsoft YaHei", sans-serif;
            background-color: #f8f9fa;
            min-height: 100vh;
        }}
        .nav-header {{
            background: linear-gradient(135deg, #165DFF, #0039CB);
            box-shadow: 0 2px 10px rgba(22, 93, 255, 0.2);
            padding: 1rem 0;
            color: white;
        }}
        .nav-title {{
            font-size: 1.8rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        .nav-tag {{
            font-size: 0.8rem;
            background: rgba(255,255,255,0.3);
            padding: 0.2rem 0.5rem;
            border-radius: 10px;
            margin-left: 1rem;
        }}
        .nav-operate {{
            display: flex;
            align-items: center;
            gap: 1rem;
            justify-content: flex-end;
        }}
        .nav-operate a {{
            color: white;
            text-decoration: none;
            padding: 0.5rem 1rem;
            border-radius: 8px;
            transition: all 0.3s ease;
        }}
        .nav-operate a:hover {{
            background-color: rgba(255,255,255,0.2);
        }}
        .card-custom {{
            background: white;
            border-radius: 12px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.05);
            border: none;
            transition: all 0.3s ease;
        }}
        .card-custom:hover {{
            transform: translateY(-3px);
        }}
        .card-header-custom {{
            background-color: #f5f7ff;
            border-bottom: 1px solid #e8edff;
            padding: 1rem 1.5rem;
            font-weight: 600;
            color: #165DFF;
        }}
        .card-body-custom {{
            padding: 1.5rem;
        }}
        .btn-primary-custom {{
            background: linear-gradient(135deg, #165DFF, #0039CB);
            border: none;
            border-radius: 8px;
            padding: 0.6rem 1.5rem;
        }}
        .btn-jp-custom {{
            background: linear-gradient(135deg, #FF6B6B, #E55353);
            border: none;
            border-radius: 8px;
            padding: 0.6rem 1.5rem;
            color: white;
        }}
        .btn-eu-custom {{
            background: linear-gradient(135deg, #00A86B, #008F5A);
            border: none;
            border-radius: 8px;
            padding: 0.6rem 1.5rem;
            color: white;
        }}
        .container-main {{
            padding: 2rem 0;
            flex-grow: 1;
        }}
        .footer {{
            padding: 1.5rem;
            text-align: center;
            color: #6c757d;
            border-top: 1px solid #e9ecef;
        }}
        .search-input {{
            border-radius: 8px;
            padding: 0.8rem 1rem;
            border: 1px solid #e8edff;
        }}
        .alert-custom {{
            border-radius: 8px;
        }}
        .badge-us {{
            background-color: #165DFF !important;
        }}
        .badge-jp {{
            background-color: #FF6B6B !important;
        }}
        .badge-eu {{
            background-color: #00A86B !important;
        }}
        .alias-tip {{
            font-size: 0.9rem;
            color: #FF6B6B;
            font-style: italic;
            margin-top: 0.3rem;
        }}
    </style>
</head>
<body class="d-flex flex-column">
    <div class="nav-header">
        <div class="container">
            <div class="row align-items-center">
                <div class="col-md-6">
                    <div class="nav-title">
                        <i class="fa fa-search"></i> 国际实体清单查询系统
                        <span class="nav-tag">美+日+欧 三数据源</span>
                    </div>
                </div>
                <div class="col-md-6">
                    <div class="nav-operate">
                        {f'<span>您好，{user["username"]}</span>' if user else ''}
                        <a href="/"><i class="fa fa-home"></i> 首页</a>
                        {('<a href="/history"><i class="fa fa-history"></i> 历史</a>' if user else '')}
                        {('<a href="/favorites"><i class="fa fa-star"></i> 收藏</a>' if user else '')}
                        {('<a href="/admin"><i class="fa fa-cog"></i> 后台</a>' if user and user['is_admin'] else '')}
                        {('<a href="/logout"><i class="fa fa-sign-out"></i> 退出</a>' if user else '<a href="/login"><i class="fa fa-sign-in"></i> 登录</a>')}
                        {('<a href="/register"><i class="fa fa-user-plus"></i> 注册</a>' if not user else '')}
                    </div>
                </div>
            </div>
        </div>
    </div>

    <div class="container-main">
        <div class="container">
            {content}
        </div>
    </div>

    <div class="footer">
        数据来源：美国商务部CSL | 日本经济产业省EUL | 欧盟FSF制裁清单 | 本系统仅用于学术研究
    </div>

    <script>
    document.addEventListener('DOMContentLoaded', function() {{
        document.querySelectorAll('.fav-btn').forEach(btn => {{
            btn.addEventListener('click', function(e) {{
                e.preventDefault();
                let id = this.dataset.id;
                let self = this;
                fetch('/api/favorite/' + id, {{ method: 'POST' }})
                .then(res => res.json())
                .then(data => {{
                    if (data.action === 'add') {{
                        self.innerHTML = '<i class="fa fa-star"></i> 已收藏';
                        self.className = 'btn btn-sm btn-danger fav-btn';
                    }} else if (data.action === 'remove') {{
                        self.innerHTML = '<i class="fa fa-star-o"></i> 收藏';
                        self.className = 'btn btn-sm btn-outline-primary fav-btn';
                    }}
                }})
            }});
        }});
    }});
    </script>
</body>
</html>
'''

# ====================== 首页 ======================
@app.route("/")
def index():
    user = get_user()
    content = '''
    <div class="row justify-content-center">
        <div class="col-md-8 col-lg-6">
            <div class="card card-custom">
                <div class="card-header-custom">
                    <i class="fa fa-database"></i> 美+日+欧 实体清单联合查询
                </div>
                <div class="card-body-custom">
                    {% with messages = get_flashed_messages(with_categories=true) %}
                    {% if messages %}
                        {% for cat, msg in messages %}
                        <div class="alert alert-{{cat}} alert-custom">{{msg}}</div>
                        {% endfor %}
                    {% endif %}
                    {% endwith %}
                    <form action="/search" method="post">
                        <div class="mb-3">
                            <label class="form-label">企业英文/中文名称/别名</label>
                            <input type="text" class="form-control search-input" name="keyword"
                                   placeholder="例如：ZHONGTIAN、中天科技、HUAWEI、ZTE、SMIC" required>
                        </div>
                        <div class="form-text text-muted mb-4">
                            <i class="fa fa-lightbulb-o"></i> 支持模糊查询，同时检索美国CSL+日本EUL+欧盟FSF清单，结果分来源标注
                        </div>
                        <button type="submit" class="btn btn-primary-custom w-100">
                            <i class="fa fa-search"></i> 联合查询
                        </button>
                    </form>
                </div>
            </div>
        </div>
    </div>
    '''
    return render_template_string(base_template(content, user))

# ====================== 搜索 ======================
@app.route("/search", methods=["POST"])
def search():
    user = get_user()
    kw = request.form.get("keyword", "").strip()
    std_kw = standard_name(kw)

    conn = sqlite3.connect("sanction.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''SELECT * FROM entity_list
                 WHERE name LIKE ? OR alt_names LIKE ?''',
              (f"%{std_kw}%", f"%{std_kw}%"))
    rows = c.fetchall()
    conn.close()

    fav_ids = get_user_favorite_ids(user["id"]) if user else []

    if user:
        conn = sqlite3.connect("sanction.db")
        c = conn.cursor()
        c.execute("INSERT INTO history VALUES (NULL,?,?,?,?)",
                  (user["id"], kw, 1 if rows else 0, now_str()))
        conn.commit()
        conn.close()

    result_html = ""
    if rows:
        for r in rows:
            alias_html = ""
            if r["alt_names"] and std_kw in standard_name(r["alt_names"]) and std_kw not in standard_name(r["name"]):
                src = {"US":"美国","JP":"日本","EU":"欧盟"}.get(r["data_source"],"未知")
                alias_html = f'''<p class="alias-tip"><i class="fa fa-exclamation-circle"></i> 该检索项于{src}清单中企业别名栏目中存在，对应全名为：{r["name"]}，请自行确认是否为该公司。</p>'''
            elif r["alt_names"]:
                alias_html = f'<p><strong>别名：</strong>{r["alt_names"]}</p>'

            addr_html = f'<p><strong>地址：</strong>{r["address"]}</p>' if r["address"] else ''
            country_html = f'<span class="badge bg-secondary me-2">{r["country"]}</span>' if r["country"] else ''
            source_label = {"US":"美国CSL","JP":"日本EUL","EU":"欧盟FSF"}.get(r["data_source"],"未知")
            source_badge = f'<span class="badge badge-{r["data_source"].lower()} me-2">{source_label}</span>'

            fav_btn = ""
            if user:
                if r["id"] in fav_ids:
                    fav_btn = f'''<button class="btn btn-sm btn-danger fav-btn" data-id="{r['id']}">
                                    <i class="fa fa-star"></i> 已收藏
                                  </button>'''
                else:
                    fav_btn = f'''<button class="btn btn-sm btn-outline-primary fav-btn" data-id="{r['id']}">
                                    <i class="fa fa-star-o"></i> 收藏
                                  </button>'''

            result_html += f'''
            <div class="card card-custom mb-3">
                <div class="card-body p-4">
                    <div class="d-flex justify-content-between">
                        <h5 class="card-title mb-2">{r["name"]}</h5>
                        {fav_btn}
                    </div>
                    {alias_html}
                    <div class="d-flex flex-wrap gap-2 mb-2">
                        {source_badge} {country_html}
                        <span class="badge bg-info">{r["list_type"]}</span>
                        <span class="badge bg-dark">{r["source"]}</span>
                    </div>
                    {addr_html}
                </div>
            </div>
            '''
    else:
        result_html = '''
        <div class="alert alert-success alert-custom text-center">
            <i class="fa fa-check-circle"></i> 未查询到相关记录
        </div>
        '''

    content = f'''
    <div class="row justify-content-center">
        <div class="col-md-10 col-lg-8">
            <div class="card card-custom">
                <div class="card-header-custom">搜索结果：{kw}</div>
                <div class="card-body-custom">
                    <p class="text-muted">共 {len(rows)} 条匹配记录</p>
                    {result_html}
                    <a href="/" class="btn btn-outline-primary">返回查询</a>
                </div>
            </div>
        </div>
    </div>
    '''
    return render_template_string(base_template(content, user))

# ====================== 登录 ======================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        pwd = hashlib.md5(request.form["pwd"].encode()).hexdigest()
        conn = sqlite3.connect("sanction.db")
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE email=? AND password=?", (email, pwd))
        u = c.fetchone()
        conn.close()
        if u:
            session["uid"] = u["id"]
            flash("登录成功！", "success")
            return redirect("/")
        else:
            flash("账号或密码错误", "danger")
    content = '''
    <div class="row justify-content-center">
        <div class="col-md-6 col-lg-4">
            <div class="card card-custom">
                <div class="card-header-custom text-center">用户登录</div>
                <div class="card-body-custom">
                    {% with messages = get_flashed_messages(with_categories=true) %}
                    {% for cat, msg in messages %}
                    <div class="alert alert-{{cat}}">{{msg}}</div>
                    {% endfor %}{% endwith %}
                    <form method="post">
                        <div class="mb-3">
                            <label>邮箱</label>
                            <input name="email" class="form-control" required>
                        </div>
                        <div class="mb-3">
                            <label>密码</label>
                            <input type="password" name="pwd" class="form-control" required>
                        </div>
                        <button class="btn btn-primary-custom w-100">登录</button>
                    </form>
                    <div class="text-center mt-2">
                        <a href="/register">注册账号</a>
                    </div>
                </div>
            </div>
        </div>
    </div>
    '''
    return render_template_string(base_template(content))

# ====================== 注册 ======================
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        user = request.form["user"].strip()
        email = request.form["email"].strip()
        pwd = hashlib.md5(request.form["pwd"].encode()).hexdigest()
        try:
            conn = sqlite3.connect("sanction.db")
            c = conn.cursor()
            c.execute("INSERT INTO users VALUES (NULL,?,?,?,0)", (user, email, pwd))
            conn.commit()
            conn.close()
            flash("注册成功，请登录", "success")
            return redirect("/login")
        except:
            flash("用户名或邮箱已存在", "danger")
    content = '''
    <div class="row justify-content-center">
        <div class="col-md-6 col-lg-4">
            <div class="card card-custom">
                <div class="card-header-custom text-center">用户注册</div>
                <div class="card-body-custom">
                    {% with messages = get_flashed_messages(with_categories=true) %}
                    {% for cat, msg in messages %}
                    <div class="alert alert-{{cat}}">{{msg}}</div>
                    {% endfor %}{% endwith %}
                    <form method="post">
                        <div class="mb-3">
                            <label>用户名</label>
                            <input name="user" class="form-control" required>
                        </div>
                        <div class="mb-3">
                            <label>邮箱</label>
                            <input name="email" class="form-control" required>
                        </div>
                        <div class="mb-3">
                            <label>密码</label>
                            <input type="password" name="pwd" class="form-control" required>
                        </div>
                        <button class="btn btn-primary-custom w-100">注册</button>
                    </form>
                </div>
            </div>
        </div>
    </div>
    '''
    return render_template_string(base_template(content))

# ====================== 退出 ======================
@app.route("/logout")
def logout():
    session.pop("uid", None)
    flash("已退出登录", "info")
    return redirect("/")

# ====================== 历史 ======================
@app.route("/history")
def history():
    user = get_user()
    if not user:
        flash("请先登录", "warning")
        return redirect("/login")
    conn = sqlite3.connect("sanction.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM history WHERE user_id=? ORDER BY id DESC", (user["id"],))
    rows = c.fetchall()
    conn.close()

    content = '''
    <div class="row justify-content-center">
        <div class="col-md-10 col-lg-8">
            <div class="card card-custom">
                <div class="card-header-custom">我的搜索历史</div>
                <div class="card-body-custom">
                    <table class="table table-hover">
                        <tr><th>关键词</th><th>时间</th><th>结果</th></tr>
    '''
    for r in rows:
        res = "有" if r["result"] else "无"
        content += f"<tr><td>{r['keyword']}</td><td>{r['create_time']}</td><td>{res}</td></tr>"
    if not rows:
        content += "<tr><td colspan=3 class=text-center>暂无记录</td></tr>"
    content += '''
                    </table>
                    <a href="/" class="btn btn-outline-primary">返回</a>
                </div>
            </div>
        </div>
    </div>
    '''
    return render_template_string(base_template(content, user))

# ====================== 后台 ======================
@app.route("/admin")
def admin():
    user = get_user()
    if not user or not user["is_admin"]:
        flash("无权限", "danger")
        return redirect("/")
    conn = sqlite3.connect("sanction.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM entity_list WHERE data_source='US'")
    us = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM entity_list WHERE data_source='JP'")
    jp = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM entity_list WHERE data_source='EU'")
    eu = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM entity_list")
    total = c.fetchone()[0]
    conn.close()

    content = f'''
    <div class="row justify-content-center">
        <div class="col-md-8 col-lg-6">
            <div class="card card-custom">
                <div class="card-header-custom text-center">管理后台</div>
                <div class="card-body-custom text-center">
                    <div class="row mb-4">
                        <div class="col-4">美国<br><b>{us}</b></div>
                        <div class="col-4">日本<br><b>{jp}</b></div>
                        <div class="col-4">欧盟<br><b>{eu}</b></div>
                    </div>
                    <a href="/admin/update/all" class="btn btn-warning w-100 mb-2">强制刷新全部数据（会重置ID，收藏可能失效）</a>
                    <a href="/" class="btn btn-outline-secondary w-100">返回首页</a>
                </div>
            </div>
        </div>
    </div>
    '''
    return render_template_string(base_template(content, user))

@app.route("/admin/update/all")
def admin_update_all():
    user = get_user()
    if not user or not user['is_admin']:
        flash("无权限", "danger")
        return redirect("/")
    flash("⚠️ 强制刷新会重置数据，收藏可能失效，请谨慎操作", "warning")
    return redirect("/admin")

# ====================== 启动 ======================
if __name__ == "__main__":
    init_database()
    load_all_data(force=False)  # ✅ 不删表、不重置ID，收藏永久保存
    app.run(debug=False, host="0.0.0.0", port=5000)