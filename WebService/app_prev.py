from flask import Flask, request, jsonify, render_template_string
from flask_sqlalchemy import SQLAlchemy
from flask_marshmallow import Marshmallow
from datetime import date, datetime
import os
import json
import ssl
import urllib.request
import urllib.error

app = Flask(__name__)

# ==============================================
# POSTGRESQL
# ==============================================
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://postgres:1234@localhost:5432/WebService'
# ==============================================

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
ma = Marshmallow(app)

# ============================================================
# НАСТРОЙКИ ОТПРАВКИ В 1С (1c_settings.json рядом с app.py)
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_1C_FILE = os.path.join(BASE_DIR, '1c_settings.json')

DEFAULT_1C_SETTINGS = {
    "base_url": "http://192.168.1.10/MyBase/hs/api",
    "timeout": 10,
    "verify_ssl": False,
    "use_auth": False,
    "login": "",
    "password": "",
    "default_method": "POST",
    "endpoints": {
        "clients": "/clients",
        "employees": "/employees",
        "warehouses": "/warehouses",
        "nomenclature": "/nomenclature",
        "goods_receipts": "/goods_receipts",
        "service_docs": "/service_docs"
    },
    "headers": {
        "Content-Type": "application/json"
    }
}

def load_1c_settings():
    try:
        if os.path.exists(SETTINGS_1C_FILE):
            with open(SETTINGS_1C_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            merged = dict(DEFAULT_1C_SETTINGS)
            merged.update(data)
            merged['endpoints'] = dict(DEFAULT_1C_SETTINGS['endpoints'])
            merged['endpoints'].update(data.get('endpoints', {}))
            merged['headers'] = dict(DEFAULT_1C_SETTINGS['headers'])
            merged['headers'].update(data.get('headers', {}))
            return merged
    except Exception:
        pass
    return json.loads(json.dumps(DEFAULT_1C_SETTINGS))

def save_1c_settings(data):
    settings = load_1c_settings()
    for key in ('base_url', 'timeout', 'verify_ssl', 'use_auth', 'login', 'password', 'default_method'):
        if key in data:
            settings[key] = data[key]
    if 'endpoints' in data and isinstance(data['endpoints'], dict):
        settings['endpoints'].update(data['endpoints'])
    if 'headers' in data and isinstance(data['headers'], dict):
        settings['headers'].update(data['headers'])
    with open(SETTINGS_1C_FILE, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    return settings

def public_1c_settings():
    s = load_1c_settings()
    s = dict(s)
    if s.get('password'):
        s['password'] = '***'
        s['has_password'] = True
    else:
        s['has_password'] = False
    return s

def do_http_to_1c(url, method, body_obj, settings, extra_headers=None):
    import base64
    data_bytes = None
    headers = dict(settings.get('headers', {}) or {})
    if extra_headers:
        headers.update(extra_headers)
    if body_obj is not None and method in ('POST', 'PUT', 'PATCH'):
        text = body_obj if isinstance(body_obj, str) else json.dumps(body_obj, ensure_ascii=False)
        data_bytes = text.encode('utf-8')
        if 'Content-Type' not in headers:
            headers['Content-Type'] = 'application/json; charset=utf-8'
    if settings.get('use_auth') and settings.get('login'):
        token = base64.b64encode(f"{settings.get('login')}:{settings.get('password', '')}".encode('utf-8')).decode('ascii')
        headers['Authorization'] = 'Basic ' + token
    req = urllib.request.Request(url, data=data_bytes, method=method, headers=headers)
    ctx = None
    if url.lower().startswith('https') and not settings.get('verify_ssl', False):
        ctx = ssl._create_unverified_context()
    timeout = int(settings.get('timeout', 10) or 10)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
            return resp.status, raw, None
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode('utf-8', errors='replace')
        except Exception:
            raw = str(e)
        return e.code, raw, None
    except Exception as e:
        return None, '', str(e)

# ============================================================
# МОДЕЛИ — строго по выгрузке из папки ВыгрузБАзы
#   Catalogs: Клиенты, Сотрудники (+ТЧ ТрудоваяДеятельность),
#             Склады, Номенклатура (иерархия + ВидНоменклатуры)
#   Documents: ПриходнаяНакладная (ТЧ Материалы),
#              ОказаниеУслуги (ТЧ ПереченьНоменклатуры)
# Подчиненные ВариантыНоменклатуры не заведены отдельным
# справочником: поле НаборСвойств в ТЧ хранится строкой (код).
# ============================================================
CODE_PREFIX = 'ВБ-'
CODE_DIGITS = 9
CODE_LEN = len(CODE_PREFIX) + CODE_DIGITS  # 12
NUMBER_LEN = 11
NUMBER_PREFIX = 'ВБ-'
NUMBER_DIGITS = NUMBER_LEN - len(NUMBER_PREFIX)  # 8, номер вида ВБ-00000001
NOM_VIDS = ('Материал', 'Услуга')

def _parse_date(v):
    if v is None or v == '':
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()[:10]
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except Exception:
        return None

# ---------- СПРАВОЧНИК: Клиенты (Код 9, Наименование 100 — Ф.И.О.) ----------
class Client(db.Model):
    __tablename__ = 'clients'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(CODE_LEN), default='')
    name = db.Column(db.String(100), nullable=False)

# ---------- СПРАВОЧНИК: Сотрудники + ТЧ ТрудоваяДеятельность ----------
class Employee(db.Model):
    __tablename__ = 'employees'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(CODE_LEN), default='')
    name = db.Column(db.String(100), nullable=False)
    employment = db.relationship('EmploymentRecord', cascade='all, delete-orphan', backref='employee')

class EmploymentRecord(db.Model):
    __tablename__ = 'employment_records'
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employees.id', ondelete='CASCADE'))
    organization = db.Column(db.String(100), default='')
    start_date = db.Column(db.Date, nullable=True)
    end_date = db.Column(db.Date, nullable=True)
    position = db.Column(db.String(100), default='')

# ---------- СПРАВОЧНИК: Склады (Код 9, Наименование 100) ----------
class Warehouse(db.Model):
    __tablename__ = 'warehouses'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(CODE_LEN), default='')
    name = db.Column(db.String(100), nullable=False)

# ---------- СПРАВОЧНИК: Номенклатура (иерархия + ВидНоменклатуры) ----------
class Nomenclature(db.Model):
    __tablename__ = 'nomenclature'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(CODE_LEN), default='')
    name = db.Column(db.String(100), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('nomenclature.id'), nullable=True)
    is_folder = db.Column(db.Boolean, default=False)
    vid = db.Column(db.String(20), default='Материал')
    parent = db.relationship('Nomenclature', remote_side='Nomenclature.id', backref='children')

# ---------- ДОКУМЕНТ: ПриходнаяНакладная (Номер 9, Склад, ТЧ Материалы) ----------
class GoodsReceipt(db.Model):
    __tablename__ = 'goods_receipts'
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(NUMBER_LEN), nullable=False)
    doc_date = db.Column(db.Date, default=date.today)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouses.id'), nullable=True)
    total_sum = db.Column(db.Numeric(15, 2), default=0)
    warehouse = db.relationship('Warehouse')
    items = db.relationship('GoodsReceiptItem', cascade='all, delete-orphan', backref='document')

class GoodsReceiptItem(db.Model):
    __tablename__ = 'goods_receipt_items'
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey('goods_receipts.id', ondelete='CASCADE'))
    material_id = db.Column(db.Integer, db.ForeignKey('nomenclature.id'), nullable=True)
    quantity = db.Column(db.Numeric(15, 3), default=0)
    price = db.Column(db.Numeric(15, 2), default=0)
    sum = db.Column(db.Numeric(15, 2), default=0)
    variant = db.Column(db.String(25), default='')
    material = db.relationship('Nomenclature')

# ---------- ДОКУМЕНТ: ОказаниеУслуги (Склад, Клиент, Мастер, ТЧ Перечень) ----------
class ServiceDoc(db.Model):
    __tablename__ = 'service_docs'
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(NUMBER_LEN), nullable=False)
    doc_date = db.Column(db.Date, default=date.today)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouses.id'), nullable=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=True)
    total_sum = db.Column(db.Numeric(15, 2), default=0)
    warehouse = db.relationship('Warehouse')
    client = db.relationship('Client')
    employee = db.relationship('Employee')
    items = db.relationship('ServiceDocItem', cascade='all, delete-orphan', backref='document')

class ServiceDocItem(db.Model):
    __tablename__ = 'service_doc_items'
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey('service_docs.id', ondelete='CASCADE'))
    nomenclature_id = db.Column(db.Integer, db.ForeignKey('nomenclature.id'), nullable=True)
    quantity = db.Column(db.Numeric(15, 3), default=0)
    price = db.Column(db.Numeric(15, 2), default=0)
    sum = db.Column(db.Numeric(15, 2), default=0)
    variant = db.Column(db.String(25), default='')
    nomenclature = db.relationship('Nomenclature')

# ---------- ЖУРНАЛ ОТПРАВОК В 1С ----------
class SendLog(db.Model):
    __tablename__ = 'send_log_1c'
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    entity = db.Column(db.String(50), default='')
    record_id = db.Column(db.Integer, nullable=True)
    method = db.Column(db.String(10), default='POST')
    url = db.Column(db.Text, default='')
    request_body = db.Column(db.Text, default='')
    response_status = db.Column(db.Integer, nullable=True)
    response_body = db.Column(db.Text, default='')
    success = db.Column(db.Boolean, default=False)
    error = db.Column(db.Text, default='')

def build_1c_body(entity, record_id):
    if entity == 'clients':
        o = Client.query.get(record_id)
        if not o:
            return None, 'Клиент не найден'
        return {'code': o.code or '', 'name': o.name}, None
    if entity == 'employees':
        o = Employee.query.get(record_id)
        if not o:
            return None, 'Сотрудник не найден'
        return {'code': o.code or '', 'name': o.name,
                'employment': [{'organization': e.organization or '',
                                'start_date': e.start_date.isoformat() if e.start_date else None,
                                'end_date': e.end_date.isoformat() if e.end_date else None,
                                'position': e.position or ''} for e in o.employment]}, None
    if entity == 'warehouses':
        o = Warehouse.query.get(record_id)
        if not o:
            return None, 'Склад не найден'
        return {'code': o.code or '', 'name': o.name}, None
    if entity == 'nomenclature':
        o = Nomenclature.query.get(record_id)
        if not o:
            return None, 'Номенклатура не найдена'
        return {'code': o.code or '', 'name': o.name, 'parent_id': o.parent_id,
                'is_folder': bool(o.is_folder), 'vid': o.vid}, None
    if entity == 'goods_receipts':
        d = GoodsReceipt.query.get(record_id)
        if not d:
            return None, 'Документ не найден'
        return {'number': d.number, 'date': d.doc_date.isoformat() if d.doc_date else None,
                'warehouse_id': d.warehouse_id,
                'items': [{'material_id': it.material_id, 'quantity': float(it.quantity or 0),
                           'price': float(it.price or 0), 'variant': it.variant or ''} for it in d.items]}, None
    if entity == 'service_docs':
        d = ServiceDoc.query.get(record_id)
        if not d:
            return None, 'Документ не найден'
        return {'number': d.number, 'date': d.doc_date.isoformat() if d.doc_date else None,
                'warehouse_id': d.warehouse_id, 'client_id': d.client_id, 'employee_id': d.employee_id,
                'items': [{'nomenclature_id': it.nomenclature_id, 'quantity': float(it.quantity or 0),
                           'price': float(it.price or 0), 'variant': it.variant or ''} for it in d.items]}, None
    return None, 'Неизвестный тип: ' + str(entity)

def ensure_code_columns():
    try:
        with db.engine.begin() as conn:
            for tbl in ('clients', 'employees', 'warehouses', 'nomenclature'):
                conn.execute(db.text(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS code VARCHAR({CODE_LEN}) DEFAULT ''"))
                conn.execute(db.text(f"UPDATE {tbl} SET code = LEFT(code, {CODE_LEN}) WHERE LENGTH(code) > {CODE_LEN}"))
                conn.execute(db.text(f"ALTER TABLE {tbl} ALTER COLUMN code TYPE VARCHAR({CODE_LEN})"))
            for tbl in ('goods_receipts', 'service_docs'):
                conn.execute(db.text(f"UPDATE {tbl} SET number = LEFT(number, {NUMBER_LEN}) WHERE LENGTH(number) > {NUMBER_LEN}"))
                conn.execute(db.text(f"ALTER TABLE {tbl} ALTER COLUMN number TYPE VARCHAR({NUMBER_LEN})"))
    except Exception as e:
        print('ensure_code_columns warning:', e)

def _check_code_len(code):
    if code and len(code) > CODE_LEN:
        return f'Код не длиннее {CODE_LEN} символов'
    return None

def _check_number_len(number):
    if number and len(number) > NUMBER_LEN:
        return f'Номер не длиннее {NUMBER_LEN} символов'
    return None

def _code_taken(model, code, exclude_id=None):
    code = (code or '').strip()
    if not code:
        return False
    q = model.query.filter(model.code == code)
    if exclude_id is not None:
        q = q.filter(model.id != exclude_id)
    return q.first() is not None

def generate_next_code(model):
    max_n = 0
    try:
        rows = db.session.query(model.code).filter(model.code.like(CODE_PREFIX + '%')).all()
        for (c,) in rows:
            if not c or not c.startswith(CODE_PREFIX):
                continue
            tail = c[len(CODE_PREFIX):]
            if tail.isdigit():
                n = int(tail)
                if n > max_n:
                    max_n = n
    except Exception:
        pass
    n = max_n + 1
    while True:
        candidate = f"{CODE_PREFIX}{n:0{CODE_DIGITS}d}"
        if not _code_taken(model, candidate):
            return candidate
        n += 1

def generate_next_number(model):
    """Следующий свободный номер документа вида ВБ-00000001."""
    max_n = 0
    try:
        rows = db.session.query(model.number).filter(model.number.like(NUMBER_PREFIX + '%')).all()
        for (c,) in rows:
            if not c or not c.startswith(NUMBER_PREFIX):
                continue
            tail = c[len(NUMBER_PREFIX):]
            if tail.isdigit():
                n = int(tail)
                if n > max_n:
                    max_n = n
    except Exception:
        pass
    n = max_n + 1
    while True:
        candidate = f"{NUMBER_PREFIX}{n:0{NUMBER_DIGITS}d}"
        if not _number_taken(model, candidate):
            return candidate
        n += 1

def _number_taken(model, number, exclude_id=None):
    number = (number or '').strip()
    if not number:
        return False
    q = model.query.filter(model.number == number)
    if exclude_id is not None:
        q = q.filter(model.id != exclude_id)
    return q.first() is not None

# ---------- СХЕМЫ ----------
class ClientSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = Client

class EmploymentSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = EmploymentRecord
        include_fk = True

class EmployeeSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = Employee
    employment = ma.Nested(EmploymentSchema, many=True)

class WarehouseSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = Warehouse

class NomenclatureSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = Nomenclature
        include_fk = True
    parent_name = ma.Method('get_parent_name')
    def get_parent_name(self, obj):
        return obj.parent.name if obj.parent else None

class GoodsReceiptItemSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = GoodsReceiptItem
        include_fk = True
    material_name = ma.Method('get_material_name')
    def get_material_name(self, obj):
        return obj.material.name if obj.material else None

class GoodsReceiptSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = GoodsReceipt
        include_fk = True
    items = ma.Nested(GoodsReceiptItemSchema, many=True)
    warehouse_name = ma.Method('get_warehouse_name')
    def get_warehouse_name(self, obj):
        return obj.warehouse.name if obj.warehouse else None

class ServiceDocItemSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = ServiceDocItem
        include_fk = True
    nomenclature_name = ma.Method('get_nom_name')
    def get_nom_name(self, obj):
        return obj.nomenclature.name if obj.nomenclature else None

class ServiceDocSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = ServiceDoc
        include_fk = True
    items = ma.Nested(ServiceDocItemSchema, many=True)
    warehouse_name = ma.Method('get_wh')
    client_name = ma.Method('get_cl')
    employee_name = ma.Method('get_emp')
    def get_wh(self, obj):
        return obj.warehouse.name if obj.warehouse else None
    def get_cl(self, obj):
        return obj.client.name if obj.client else None
    def get_emp(self, obj):
        return obj.employee.name if obj.employee else None

class SendLogSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = SendLog

# ============================================================
# КЛИЕНТЫ  (Catalog.Клиенты: Code + Наименование/Ф.И.О.)
# ============================================================
@app.route('/api/clients', methods=['GET'])
def get_clients():
    return jsonify(ClientSchema(many=True).dump(Client.query.all()))

@app.route('/api/clients/<int:id>', methods=['GET'])
def get_client(id):
    return jsonify(ClientSchema().dump(Client.query.get_or_404(id)))

@app.route('/api/clients', methods=['POST'])
def add_client():
    data = request.get_json(force=True, silent=True) or {}
    try:
        if not (data.get('name') or '').strip():
            return jsonify({'error': 'Поле name (Ф.И.О.) обязательно'}), 400
        code = (data.get('code') or '').strip()
        if not code:
            code = generate_next_code(Client)
        err = _check_code_len(code)
        if err:
            return jsonify({'error': err}), 400
        if _code_taken(Client, code):
            return jsonify({'error': f'Код {code} уже занят'}), 400
        item = Client(code=code, name=data['name'].strip()[:100])
        db.session.add(item)
        db.session.commit()
        return jsonify(ClientSchema().dump(item)), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

def _apply_client_fields(item, data, exclude_id=None):
    if 'code' in data:
        code = (data.get('code') or '').strip()
        err = _check_code_len(code)
        if err:
            return err
        if code and _code_taken(Client, code, exclude_id):
            return f'Код {code} уже занят'
        item.code = code
    if 'name' in data:
        if not (data.get('name') or '').strip():
            return 'Поле name (Ф.И.О.) обязательно'
        item.name = data['name'].strip()[:100]
    return None

@app.route('/api/clients/<int:id>', methods=['PUT', 'PATCH'])
def update_client(id):
    item = Client.query.get_or_404(id)
    data = request.get_json(force=True, silent=True) or {}
    try:
        err = _apply_client_fields(item, data, exclude_id=id)
        if err:
            return jsonify({'error': err}), 400
        db.session.commit()
        return jsonify(ClientSchema().dump(item))
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/clients/<int:id>', methods=['DELETE'])
def delete_client(id):
    item = Client.query.get_or_404(id)
    try:
        db.session.delete(item)
        db.session.commit()
        return jsonify({'message': 'Клиент удалён', 'id': id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/clients/by-code/<code>', methods=['GET'])
def get_client_by_code(code):
    item = Client.query.filter_by(code=code).first_or_404()
    return jsonify(ClientSchema().dump(item))

@app.route('/api/clients/by-code/<code>', methods=['PUT', 'PATCH'])
def update_client_by_code(code):
    item = Client.query.filter_by(code=code).first_or_404()
    data = request.get_json(force=True, silent=True) or {}
    try:
        err = _apply_client_fields(item, data, exclude_id=item.id)
        if err:
            return jsonify({'error': err}), 400
        db.session.commit()
        return jsonify(ClientSchema().dump(item))
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/clients/by-code/<code>', methods=['DELETE'])
def delete_client_by_code(code):
    item = Client.query.filter_by(code=code).first_or_404()
    try:
        db.session.delete(item)
        db.session.commit()
        return jsonify({'message': 'Клиент удалён', 'code': code})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

# ============================================================
# СОТРУДНИКИ (Catalog.Сотрудники + ТЧ ТрудоваяДеятельность)
# ============================================================
def _replace_employment(emp, rows):
    EmploymentRecord.query.filter_by(employee_id=emp.id).delete()
    for r in rows or []:
        db.session.add(EmploymentRecord(
            employee_id=emp.id,
            organization=(r.get('organization') or '')[:100],
            start_date=_parse_date(r.get('start_date') or r.get('nachalo')),
            end_date=_parse_date(r.get('end_date') or r.get('okonchanie')),
            position=(r.get('position') or r.get('dolzhnost') or '')[:100]))

@app.route('/api/employees', methods=['GET'])
def get_employees():
    return jsonify(EmployeeSchema(many=True).dump(Employee.query.all()))

@app.route('/api/employees/<int:id>', methods=['GET'])
def get_employee(id):
    return jsonify(EmployeeSchema().dump(Employee.query.get_or_404(id)))

@app.route('/api/employees', methods=['POST'])
def add_employee():
    data = request.get_json(force=True, silent=True) or {}
    try:
        if not (data.get('name') or '').strip():
            return jsonify({'error': 'Поле name обязательно'}), 400
        code = (data.get('code') or '').strip()
        if not code:
            code = generate_next_code(Employee)
        err = _check_code_len(code)
        if err:
            return jsonify({'error': err}), 400
        if _code_taken(Employee, code):
            return jsonify({'error': f'Код {code} уже занят'}), 400
        item = Employee(code=code, name=data['name'].strip()[:100])
        db.session.add(item)
        db.session.flush()
        _replace_employment(item, data.get('employment'))
        db.session.commit()
        return jsonify(EmployeeSchema().dump(item)), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/employees/<int:id>', methods=['PUT', 'PATCH'])
def update_employee(id):
    item = Employee.query.get_or_404(id)
    data = request.get_json(force=True, silent=True) or {}
    try:
        if 'code' in data:
            code = (data.get('code') or '').strip()
            err = _check_code_len(code)
            if err:
                return jsonify({'error': err}), 400
            if code and _code_taken(Employee, code, id):
                return jsonify({'error': f'Код {code} уже занят'}), 400
            item.code = code
        if 'name' in data:
            if not (data.get('name') or '').strip():
                return jsonify({'error': 'Поле name обязательно'}), 400
            item.name = data['name'].strip()[:100]
        if 'employment' in data:
            _replace_employment(item, data['employment'] or [])
        db.session.commit()
        return jsonify(EmployeeSchema().dump(item))
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/employees/<int:id>', methods=['DELETE'])
def delete_employee(id):
    item = Employee.query.get_or_404(id)
    try:
        db.session.delete(item)
        db.session.commit()
        return jsonify({'message': 'Сотрудник удалён', 'id': id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/employees/by-code/<code>', methods=['GET'])
def get_employee_by_code(code):
    item = Employee.query.filter_by(code=code).first_or_404()
    return jsonify(EmployeeSchema().dump(item))

@app.route('/api/employees/by-code/<code>', methods=['PUT', 'PATCH'])
def update_employee_by_code(code):
    item = Employee.query.filter_by(code=code).first_or_404()
    data = request.get_json(force=True, silent=True) or {}
    try:
        if 'code' in data:
            nc = (data.get('code') or '').strip()
            err = _check_code_len(nc)
            if err:
                return jsonify({'error': err}), 400
            if nc and _code_taken(Employee, nc, item.id):
                return jsonify({'error': f'Код {nc} уже занят'}), 400
            item.code = nc
        if 'name' in data:
            item.name = (data['name'] or '').strip()[:100]
        if 'employment' in data:
            _replace_employment(item, data['employment'] or [])
        db.session.commit()
        return jsonify(EmployeeSchema().dump(item))
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/employees/by-code/<code>', methods=['DELETE'])
def delete_employee_by_code(code):
    item = Employee.query.filter_by(code=code).first_or_404()
    try:
        db.session.delete(item)
        db.session.commit()
        return jsonify({'message': 'Сотрудник удалён', 'code': code})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

# ============================================================
# СКЛАДЫ (Catalog.Склады: Code + Наименование)
# ============================================================
@app.route('/api/warehouses', methods=['GET'])
def get_warehouses():
    return jsonify(WarehouseSchema(many=True).dump(Warehouse.query.all()))

@app.route('/api/warehouses/<int:id>', methods=['GET'])
def get_warehouse(id):
    return jsonify(WarehouseSchema().dump(Warehouse.query.get_or_404(id)))

@app.route('/api/warehouses', methods=['POST'])
def add_warehouse():
    data = request.get_json(force=True, silent=True) or {}
    try:
        if not (data.get('name') or '').strip():
            return jsonify({'error': 'Поле name обязательно'}), 400
        code = (data.get('code') or '').strip()
        if not code:
            code = generate_next_code(Warehouse)
        err = _check_code_len(code)
        if err:
            return jsonify({'error': err}), 400
        if _code_taken(Warehouse, code):
            return jsonify({'error': f'Код {code} уже занят'}), 400
        item = Warehouse(code=code, name=data['name'].strip()[:100])
        db.session.add(item)
        db.session.commit()
        return jsonify(WarehouseSchema().dump(item)), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

def _apply_wh_fields(item, data, exclude_id=None):
    if 'code' in data:
        code = (data.get('code') or '').strip()
        err = _check_code_len(code)
        if err:
            return err
        if code and _code_taken(Warehouse, code, exclude_id):
            return f'Код {code} уже занят'
        item.code = code
    if 'name' in data:
        if not (data.get('name') or '').strip():
            return 'Поле name обязательно'
        item.name = data['name'].strip()[:100]
    return None

@app.route('/api/warehouses/<int:id>', methods=['PUT', 'PATCH'])
def update_warehouse(id):
    item = Warehouse.query.get_or_404(id)
    data = request.get_json(force=True, silent=True) or {}
    try:
        err = _apply_wh_fields(item, data, exclude_id=id)
        if err:
            return jsonify({'error': err}), 400
        db.session.commit()
        return jsonify(WarehouseSchema().dump(item))
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/warehouses/<int:id>', methods=['DELETE'])
def delete_warehouse(id):
    item = Warehouse.query.get_or_404(id)
    try:
        db.session.delete(item)
        db.session.commit()
        return jsonify({'message': 'Склад удалён', 'id': id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/warehouses/by-code/<code>', methods=['GET'])
def get_warehouse_by_code(code):
    item = Warehouse.query.filter_by(code=code).first_or_404()
    return jsonify(WarehouseSchema().dump(item))

@app.route('/api/warehouses/by-code/<code>', methods=['PUT', 'PATCH'])
def update_warehouse_by_code(code):
    item = Warehouse.query.filter_by(code=code).first_or_404()
    data = request.get_json(force=True, silent=True) or {}
    try:
        err = _apply_wh_fields(item, data, exclude_id=item.id)
        if err:
            return jsonify({'error': err}), 400
        db.session.commit()
        return jsonify(WarehouseSchema().dump(item))
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/warehouses/by-code/<code>', methods=['DELETE'])
def delete_warehouse_by_code(code):
    item = Warehouse.query.filter_by(code=code).first_or_404()
    try:
        db.session.delete(item)
        db.session.commit()
        return jsonify({'message': 'Склад удалён', 'code': code})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

# ============================================================
# НОМЕНКЛАТУРА (иерархия: parent/is_folder + ВидНоменклатуры)
# ============================================================
def _apply_nom_fields(item, data, exclude_id=None):
    if 'code' in data:
        code = (data.get('code') or '').strip()
        err = _check_code_len(code)
        if err:
            return err
        if code and _code_taken(Nomenclature, code, exclude_id):
            return f'Код {code} уже занят'
        item.code = code
    if 'name' in data:
        if not (data.get('name') or '').strip():
            return 'Поле name обязательно'
        item.name = data['name'].strip()[:100]
    if 'parent_id' in data:
        pid = data.get('parent_id')
        if pid in ('', 0):
            pid = None
        if pid is not None:
            try:
                pid = int(pid)
            except Exception:
                return 'parent_id должен быть числом'
            if exclude_id is not None and pid == exclude_id:
                return 'Элемент не может быть родителем сам себе'
            if not Nomenclature.query.get(pid):
                return 'Родитель (группа) не найден'
        item.parent_id = pid
    if 'is_folder' in data:
        item.is_folder = bool(data.get('is_folder'))
    if 'vid' in data:
        v = (data.get('vid') or '').strip()
        if v and v not in NOM_VIDS:
            return 'vid должен быть Материал или Услуга'
        if v:
            item.vid = v
    return None

@app.route('/api/nomenclature', methods=['GET'])
def get_nomenclature_list():
    return jsonify(NomenclatureSchema(many=True).dump(Nomenclature.query.all()))

@app.route('/api/nomenclature/<int:id>', methods=['GET'])
def get_nomenclature_one(id):
    return jsonify(NomenclatureSchema().dump(Nomenclature.query.get_or_404(id)))

@app.route('/api/nomenclature', methods=['POST'])
def add_nomenclature():
    data = request.get_json(force=True, silent=True) or {}
    try:
        if not (data.get('name') or '').strip():
            return jsonify({'error': 'Поле name обязательно'}), 400
        code = (data.get('code') or '').strip()
        if not code:
            code = generate_next_code(Nomenclature)
        err = _check_code_len(code)
        if err:
            return jsonify({'error': err}), 400
        if _code_taken(Nomenclature, code):
            return jsonify({'error': f'Код {code} уже занят'}), 400
        vid = (data.get('vid') or 'Материал').strip() or 'Материал'
        if vid not in NOM_VIDS:
            return jsonify({'error': 'vid должен быть Материал или Услуга'}), 400
        item = Nomenclature(code=code, name=data['name'].strip()[:100],
                            is_folder=bool(data.get('is_folder', False)), vid=vid)
        db.session.add(item)
        db.session.flush()
        err = _apply_nom_fields(item, {'parent_id': data.get('parent_id')} if 'parent_id' in data else {}, exclude_id=item.id)
        if err:
            db.session.rollback()
            return jsonify({'error': err}), 400
        db.session.commit()
        return jsonify(NomenclatureSchema().dump(item)), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/nomenclature/<int:id>', methods=['PUT', 'PATCH'])
def update_nomenclature(id):
    item = Nomenclature.query.get_or_404(id)
    data = request.get_json(force=True, silent=True) or {}
    try:
        err = _apply_nom_fields(item, data, exclude_id=id)
        if err:
            return jsonify({'error': err}), 400
        db.session.commit()
        return jsonify(NomenclatureSchema().dump(item))
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/nomenclature/<int:id>', methods=['DELETE'])
def delete_nomenclature(id):
    item = Nomenclature.query.get_or_404(id)
    try:
        db.session.delete(item)
        db.session.commit()
        return jsonify({'message': 'Номенклатура удалена', 'id': id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/nomenclature/by-code/<code>', methods=['GET'])
def get_nomenclature_by_code(code):
    item = Nomenclature.query.filter_by(code=code).first_or_404()
    return jsonify(NomenclatureSchema().dump(item))

@app.route('/api/nomenclature/by-code/<code>', methods=['PUT', 'PATCH'])
def update_nomenclature_by_code(code):
    item = Nomenclature.query.filter_by(code=code).first_or_404()
    data = request.get_json(force=True, silent=True) or {}
    try:
        err = _apply_nom_fields(item, data, exclude_id=item.id)
        if err:
            return jsonify({'error': err}), 400
        db.session.commit()
        return jsonify(NomenclatureSchema().dump(item))
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/nomenclature/by-code/<code>', methods=['DELETE'])
def delete_nomenclature_by_code(code):
    item = Nomenclature.query.filter_by(code=code).first_or_404()
    try:
        db.session.delete(item)
        db.session.commit()
        return jsonify({'message': 'Номенклатура удалена', 'code': code})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

# ============================================================
# ДОКУМЕНТ: ПриходнаяНакладная (Склад + ТЧ Материалы)
# ============================================================
def _replace_goods_items(doc, items_data):
    GoodsReceiptItem.query.filter_by(document_id=doc.id).delete()
    total = 0
    for r in items_data or []:
        if not r.get('material_id'):
            raise ValueError('В строке ТЧ не указан material_id')
        q = float(r.get('quantity', 0) or 0)
        p = float(r.get('price', 0) or 0)
        s = round(q * p, 2)
        total += s
        db.session.add(GoodsReceiptItem(document_id=doc.id, material_id=r['material_id'],
                                        quantity=q, price=p, sum=s,
                                        variant=(r.get('variant') or '')[:25]))
    doc.total_sum = round(total, 2)

@app.route('/api/goods_receipts', methods=['GET'])
def get_goods_receipts():
    return jsonify(GoodsReceiptSchema(many=True).dump(GoodsReceipt.query.all()))

@app.route('/api/goods_receipts/<int:id>', methods=['GET'])
def get_goods_receipt(id):
    return jsonify(GoodsReceiptSchema().dump(GoodsReceipt.query.get_or_404(id)))

@app.route('/api/goods_receipts', methods=['POST'])
def add_goods_receipt():
    data = request.get_json(force=True, silent=True) or {}
    try:
        number = (data.get('number') or '').strip()
        if not number:
            number = generate_next_number(GoodsReceipt)
        err = _check_number_len(number)
        if err:
            return jsonify({'error': err}), 400
        if _number_taken(GoodsReceipt, number):
            return jsonify({'error': f'Номер {number} уже занят'}), 400
        doc = GoodsReceipt(number=number,
                           doc_date=_parse_date(data.get('date') or data.get('doc_date')) or date.today(),
                           warehouse_id=data.get('warehouse_id'))
        db.session.add(doc)
        db.session.flush()
        _replace_goods_items(doc, data.get('items') or [])
        db.session.commit()
        return jsonify(GoodsReceiptSchema().dump(doc)), 201
    except ValueError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/goods_receipts/<int:id>', methods=['PUT', 'PATCH'])
def update_goods_receipt(id):
    doc = GoodsReceipt.query.get_or_404(id)
    data = request.get_json(force=True, silent=True) or {}
    try:
        if 'number' in data:
            number = (data.get('number') or '').strip()
            if not number:
                return jsonify({'error': 'Поле number обязательно'}), 400
            err = _check_number_len(number)
            if err:
                return jsonify({'error': err}), 400
            if _number_taken(GoodsReceipt, number, id):
                return jsonify({'error': f'Номер {number} уже занят'}), 400
            doc.number = number
        if 'date' in data or 'doc_date' in data:
            doc.doc_date = _parse_date(data.get('date') or data.get('doc_date')) or doc.doc_date
        if 'warehouse_id' in data:
            doc.warehouse_id = data['warehouse_id']
        if 'items' in data:
            _replace_goods_items(doc, data['items'] or [])
        db.session.commit()
        return jsonify(GoodsReceiptSchema().dump(doc))
    except ValueError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/goods_receipts/<int:id>', methods=['DELETE'])
def delete_goods_receipt(id):
    doc = GoodsReceipt.query.get_or_404(id)
    try:
        db.session.delete(doc)
        db.session.commit()
        return jsonify({'message': 'Документ удалён', 'id': id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/goods_receipts/by-number/<number>', methods=['GET'])
def get_goods_by_number(number):
    doc = GoodsReceipt.query.filter_by(number=number).first_or_404()
    return jsonify(GoodsReceiptSchema().dump(doc))

@app.route('/api/goods_receipts/by-number/<number>', methods=['PUT', 'PATCH'])
def update_goods_by_number(number):
    doc = GoodsReceipt.query.filter_by(number=number).first_or_404()
    data = request.get_json(force=True, silent=True) or {}
    try:
        if 'number' in data:
            nn = (data.get('number') or '').strip()
            if _number_taken(GoodsReceipt, nn, doc.id):
                return jsonify({'error': f'Номер {nn} уже занят'}), 400
            doc.number = nn
        if 'date' in data or 'doc_date' in data:
            doc.doc_date = _parse_date(data.get('date') or data.get('doc_date')) or doc.doc_date
        if 'warehouse_id' in data:
            doc.warehouse_id = data['warehouse_id']
        if 'items' in data:
            _replace_goods_items(doc, data['items'] or [])
        db.session.commit()
        return jsonify(GoodsReceiptSchema().dump(doc))
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/goods_receipts/by-number/<number>', methods=['DELETE'])
def delete_goods_by_number(number):
    doc = GoodsReceipt.query.filter_by(number=number).first_or_404()
    try:
        db.session.delete(doc)
        db.session.commit()
        return jsonify({'message': 'Документ удалён', 'number': number})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

# ============================================================
# ДОКУМЕНТ: ОказаниеУслуги (Склад, Клиент, Мастер + ТЧ Перечень)
# ============================================================
def _replace_service_items(doc, items_data):
    ServiceDocItem.query.filter_by(document_id=doc.id).delete()
    total = 0
    for r in items_data or []:
        if not r.get('nomenclature_id'):
            raise ValueError('В строке ТЧ не указан nomenclature_id')
        q = float(r.get('quantity', 0) or 0)
        p = float(r.get('price', 0) or 0)
        s = round(q * p, 2)
        total += s
        db.session.add(ServiceDocItem(document_id=doc.id, nomenclature_id=r['nomenclature_id'],
                                      quantity=q, price=p, sum=s,
                                      variant=(r.get('variant') or '')[:25]))
    doc.total_sum = round(total, 2)

@app.route('/api/service_docs', methods=['GET'])
def get_service_docs():
    return jsonify(ServiceDocSchema(many=True).dump(ServiceDoc.query.all()))

@app.route('/api/service_docs/<int:id>', methods=['GET'])
def get_service_doc(id):
    return jsonify(ServiceDocSchema().dump(ServiceDoc.query.get_or_404(id)))

@app.route('/api/service_docs', methods=['POST'])
def add_service_doc():
    data = request.get_json(force=True, silent=True) or {}
    try:
        number = (data.get('number') or '').strip()
        if not number:
            number = generate_next_number(ServiceDoc)
        err = _check_number_len(number)
        if err:
            return jsonify({'error': err}), 400
        if _number_taken(ServiceDoc, number):
            return jsonify({'error': f'Номер {number} уже занят'}), 400
        doc = ServiceDoc(number=number,
                         doc_date=_parse_date(data.get('date') or data.get('doc_date')) or date.today(),
                         warehouse_id=data.get('warehouse_id'),
                         client_id=data.get('client_id'),
                         employee_id=data.get('employee_id') or data.get('master_id'))
        db.session.add(doc)
        db.session.flush()
        _replace_service_items(doc, data.get('items') or [])
        db.session.commit()
        return jsonify(ServiceDocSchema().dump(doc)), 201
    except ValueError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/service_docs/<int:id>', methods=['PUT', 'PATCH'])
def update_service_doc(id):
    doc = ServiceDoc.query.get_or_404(id)
    data = request.get_json(force=True, silent=True) or {}
    try:
        if 'number' in data:
            number = (data.get('number') or '').strip()
            if not number:
                return jsonify({'error': 'Поле number обязательно'}), 400
            err = _check_number_len(number)
            if err:
                return jsonify({'error': err}), 400
            if _number_taken(ServiceDoc, number, id):
                return jsonify({'error': f'Номер {number} уже занят'}), 400
            doc.number = number
        if 'date' in data or 'doc_date' in data:
            doc.doc_date = _parse_date(data.get('date') or data.get('doc_date')) or doc.doc_date
        for f in ('warehouse_id', 'client_id', 'employee_id', 'master_id'):
            if f in data:
                if f == 'master_id':
                    doc.employee_id = data[f]
                else:
                    setattr(doc, f, data[f])
        if 'items' in data:
            _replace_service_items(doc, data['items'] or [])
        db.session.commit()
        return jsonify(ServiceDocSchema().dump(doc))
    except ValueError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/service_docs/<int:id>', methods=['DELETE'])
def delete_service_doc(id):
    doc = ServiceDoc.query.get_or_404(id)
    try:
        db.session.delete(doc)
        db.session.commit()
        return jsonify({'message': 'Документ удалён', 'id': id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/service_docs/by-number/<number>', methods=['GET'])
def get_service_by_number(number):
    doc = ServiceDoc.query.filter_by(number=number).first_or_404()
    return jsonify(ServiceDocSchema().dump(doc))

@app.route('/api/service_docs/by-number/<number>', methods=['PUT', 'PATCH'])
def update_service_by_number(number):
    doc = ServiceDoc.query.filter_by(number=number).first_or_404()
    data = request.get_json(force=True, silent=True) or {}
    try:
        if 'number' in data:
            nn = (data.get('number') or '').strip()
            if _number_taken(ServiceDoc, nn, doc.id):
                return jsonify({'error': f'Номер {nn} уже занят'}), 400
            doc.number = nn
        if 'date' in data or 'doc_date' in data:
            doc.doc_date = _parse_date(data.get('date') or data.get('doc_date')) or doc.doc_date
        for f in ('warehouse_id', 'client_id', 'employee_id', 'master_id'):
            if f in data:
                if f == 'master_id':
                    doc.employee_id = data[f]
                else:
                    setattr(doc, f, data[f])
        if 'items' in data:
            _replace_service_items(doc, data['items'] or [])
        db.session.commit()
        return jsonify(ServiceDocSchema().dump(doc))
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/service_docs/by-number/<number>', methods=['DELETE'])
def delete_service_by_number(number):
    doc = ServiceDoc.query.filter_by(number=number).first_or_404()
    try:
        db.session.delete(doc)
        db.session.commit()
        return jsonify({'message': 'Документ удалён', 'number': number})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

# ============================================================
# СЛЕДУЮЩИЙ СВОБОДНЫЙ КОД
# ============================================================
@app.route('/api/next-code/<entity>', methods=['GET'])
def get_next_code(entity):
    mapping = {
        'clients': Client,
        'employees': Employee,
        'warehouses': Warehouse,
        'nomenclature': Nomenclature,
    }
    model = mapping.get(entity)
    if model is None:
        return jsonify({'error': 'Неизвестный тип: ' + str(entity)}), 400
    return jsonify({'code': generate_next_code(model)})

@app.route('/api/next-number/<entity>', methods=['GET'])
def get_next_number(entity):
    mapping = {
        'goods_receipts': GoodsReceipt,
        'service_docs': ServiceDoc,
    }
    model = mapping.get(entity)
    if model is None:
        return jsonify({'error': 'Неизвестный тип: ' + str(entity)}), 400
    return jsonify({'number': generate_next_number(model)})

# ============================================================
# ИНТЕГРАЦИЯ С 1С
# ============================================================
@app.route('/api/settings_1c', methods=['GET'])
def get_settings_1c():
    return jsonify(public_1c_settings())

@app.route('/api/settings_1c', methods=['PUT'])
def update_settings_1c():
    data = request.get_json(force=True, silent=True) or {}
    if data.get('password') in (None, '', '***'):
        data.pop('password', None)
    try:
        if 'timeout' in data:
            data['timeout'] = int(data['timeout'])
    except Exception:
        return jsonify({'error': 'timeout должен быть числом'}), 400
    save_1c_settings(data)
    return jsonify(public_1c_settings())

@app.route('/api/outbox_log', methods=['GET'])
def get_outbox_log():
    try:
        limit = int(request.args.get('limit', 100))
    except Exception:
        limit = 100
    logs = SendLog.query.order_by(SendLog.id.desc()).limit(min(limit, 500)).all()
    return jsonify(SendLogSchema(many=True).dump(logs))

@app.route('/api/outbox_log/<int:id>', methods=['DELETE'])
def delete_log_item(id):
    item = SendLog.query.get_or_404(id)
    db.session.delete(item)
    db.session.commit()
    return jsonify({'message': 'Запись журнала удалена', 'id': id})

@app.route('/api/outbox_log', methods=['DELETE'])
def clear_log():
    SendLog.query.delete()
    db.session.commit()
    return jsonify({'message': 'Журнал очищен'})

@app.route('/api/test_1c', methods=['POST'])
def test_1c():
    settings = load_1c_settings()
    data = request.get_json(force=True, silent=True) or {}
    base_url = (data.get('base_url') or settings.get('base_url', '')).strip()
    if not base_url:
        return jsonify({'success': False, 'error': 'Не задан base_url'}), 400
    tmp = dict(settings)
    tmp['base_url'] = base_url
    if data.get('login') is not None:
        tmp['login'] = data.get('login')
        tmp['password'] = data.get('password', '')
        tmp['use_auth'] = bool(data.get('use_auth', tmp.get('use_auth')))
    status, body, err = do_http_to_1c(base_url, 'GET', None, tmp)
    if err:
        return jsonify({'success': False, 'error': err, 'url': base_url})
    return jsonify({'success': 200 <= (status or 500) < 400, 'status': status, 'body': body[:2000], 'url': base_url})

@app.route('/api/build_1c_body', methods=['GET'])
def api_build_1c_body():
    entity = request.args.get('entity', '')
    record_id = request.args.get('record_id', type=int)
    if not entity or not record_id:
        return jsonify({'error': 'Нужно entity и record_id'}), 400
    body, err = build_1c_body(entity, record_id)
    if err:
        return jsonify({'error': err}), 404
    return jsonify(body)

@app.route('/api/send_to_1c', methods=['POST'])
def send_to_1c():
    data = request.get_json(force=True, silent=True) or {}
    settings = load_1c_settings()
    entity = data.get('entity', 'custom')
    record_id = data.get('record_id')
    method = (data.get('method') or settings.get('default_method') or 'POST').upper()
    path = data.get('path')
    if not path:
        path = (settings.get('endpoints', {}) or {}).get(entity, '/custom')
    base_url = (data.get('base_url') or settings.get('base_url', '')).strip().rstrip('/')
    if not base_url:
        return jsonify({'success': False, 'error': 'Не задан Base URL 1С в настройках'}), 400
    url = base_url + '/' + path.lstrip('/')
    body = data.get('body')
    if body is None and entity != 'custom' and record_id:
        body, err = build_1c_body(entity, record_id)
        if err:
            return jsonify({'success': False, 'error': err}), 404
    if method in ('GET', 'DELETE') and body is None:
        body = None
    req_text = '' if body is None else (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False))
    status, resp_text, err = do_http_to_1c(url, method, body, settings)
    success = err is None and status is not None and 200 <= status < 300
    try:
        log = SendLog(entity=entity, record_id=record_id, method=method, url=url,
                      request_body=req_text[:8000] if isinstance(req_text, str) else '',
                      response_status=status, response_body=(resp_text or '')[:8000],
                      success=success, error=(err or ''))
        db.session.add(log)
        db.session.commit()
        log_id = log.id
    except Exception:
        db.session.rollback()
        log_id = None
    code = 200 if success else 502
    return jsonify({'success': success, 'status': status, 'url': url, 'method': method,
                    'response': resp_text, 'error': err, 'log_id': log_id}), code

# ---------- ГЛАВНАЯ СТРАНИЦА ----------
@app.route('/')
def index():
    return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Веб-сервис</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: "Segoe UI", Arial, sans-serif; background: #f0f2f5; padding: 20px; }
        .container { max-width: 1400px; margin: 0 auto; }
        h1 { color: #1a73e8; margin-bottom: 10px; border-bottom: 3px solid #1a73e8; padding-bottom: 10px; }
        .subtitle { color: #666; margin-bottom: 20px; }
        .info-box { background: #e9ecef; padding: 15px; border-radius: 8px; margin-bottom: 20px; display: flex; flex-wrap: wrap; gap: 20px; }
        .info-box span { background: white; padding: 8px 16px; border-radius: 4px; }
        .warning { background: #fff3cd; border: 1px solid #ffc107; padding: 12px 16px; border-radius: 8px; margin-bottom: 20px; }
        .card { background: white; border-radius: 8px; padding: 15px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; flex-wrap: wrap; gap: 10px; }
        .card-header h2 { color: #333; font-size: 18px; }
        .badge { background: #1a73e8; color: white; padding: 4px 12px; border-radius: 20px; font-size: 12px; }
        .btn { background: #1a73e8; color: white; border: none; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; }
        .btn:hover { background: #1557b0; }
        .btn.green { background: #28a745; }
        .btn.green:hover { background: #218838; }
        .btn.orange { background: #fd7e14; }
        .btn.orange:hover { background: #e67a00; }
        .btn.red { background: #dc3545; }
        .btn.red:hover { background: #c82333; }
        .btn-sm { padding: 2px 8px; font-size: 11px; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th { background: #f8f9fa; text-align: left; padding: 8px 10px; border-bottom: 2px solid #dee2e6; }
        td { padding: 6px 10px; border-bottom: 1px solid #dee2e6; }
        tr:hover { background: #f8f9fa; }
        .loading { text-align: center; padding: 20px; color: #666; }
        .status.empty { background: #e2e3e5; color: #383d41; padding: 8px 12px; border-radius: 4px; }
        .status.error { background: #f8d7da; color: #721c24; padding: 8px 12px; border-radius: 4px; }
        .template-block { background: #f8f9fa; border: 1px solid #dee2e6; border-radius: 4px; padding: 10px; margin-top: 8px; display: none; }
        .template-block pre { font-family: "Courier New", monospace; font-size: 12px; white-space: pre-wrap; word-wrap: break-word; margin: 0; }
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 1000; justify-content: center; align-items: center; }
        .modal-content { background: white; padding: 25px; border-radius: 8px; max-width: 800px; width: 95%; max-height: 85vh; overflow-y: auto; }
        .modal-content h3 { margin-bottom: 15px; color: #1a73e8; }
        .modal-content .close { float: right; font-size: 28px; font-weight: bold; cursor: pointer; color: #999; }
        .modal-content .close:hover { color: red; }
        .modal-content .json-view { background: #f8f9fa; padding: 15px; border-radius: 4px; font-family: "Courier New", monospace; font-size: 13px; white-space: pre-wrap; max-height: 60vh; overflow: auto; border: 1px solid #dee2e6; }
        .modal-content .doc-detail-header { display: flex; gap: 20px; flex-wrap: wrap; background: #e9ecef; padding: 10px 15px; border-radius: 4px; margin-bottom: 15px; }
        .items-table { margin-top: 10px; width: 100%; }
        .items-table th { background: #e9ecef; }
        .nav-list { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 20px; }
        .nav-item { background: white; padding: 10px 20px; border-radius: 8px; cursor: pointer; box-shadow: 0 2px 4px rgba(0,0,0,0.1); border: 2px solid transparent; transition: all 0.2s; }
        .nav-item:hover { border-color: #1a73e8; background: #f8f9ff; }
        .nav-item.active { border-color: #1a73e8; background: #e8f0fe; }
        .nav-item .name { font-weight: bold; }
        .page { display: none; }
        .page.active { display: block; }
        .section-label { font-size: 14px; font-weight: bold; color: #666; margin: 15px 0 10px 0; }
        .form-grid { display: grid; grid-template-columns: 180px 1fr; gap: 8px 12px; align-items: center; margin: 10px 0; }
        .form-grid label { font-size: 13px; color: #333; font-weight: bold; }
        .form-grid input, .form-grid select, .form-grid textarea { padding: 6px 10px; border: 1px solid #ced4da; border-radius: 4px; font-size: 13px; width: 100%; }
        .form-grid textarea { font-family: "Courier New", monospace; resize: vertical; }
        .row-flex { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
        .hint { font-size: 12px; color: #666; }
        .ok-text { color: #28a745; font-size: 13px; }
        .err-text { color: #dc3545; font-size: 13px; }
        .tag-ok { background: #d4edda; color: #155724; padding: 2px 8px; border-radius: 10px; font-size: 11px; }
        .tag-fail { background: #f8d7da; color: #721c24; padding: 2px 8px; border-radius: 10px; font-size: 11px; }
        .nav-item.send1c { border-color: #28a745; }
        .nav-item.send1c.active { background: #e6f4ea; border-color: #28a745; }
        .add-form { background: #f8f9ff; border: 1px dashed #1a73e8; border-radius: 6px; padding: 12px; margin-top: 12px; }
        .add-form h4 { color: #1a73e8; margin-bottom: 8px; font-size: 14px; }
        .add-form .form-grid { grid-template-columns: 140px 1fr; }
        .code-row { display: flex; gap: 6px; }
        .code-row input { flex: 1; font-family: "Courier New", monospace; }
        .tch-edit td input, .tch-edit td select { width: 100%; padding: 4px 6px; border: 1px solid #ced4da; border-radius: 4px; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Веб-сервис</h1>
        <div class="subtitle">Справочники и документы — как в 1С (выгрузка из папки ВыгрузБАзы)</div>
        <div class="info-box">
            <span>База: <strong>PostgreSQL</strong></span>
            <span>API: <strong>/api/...</strong></span>
        </div>
        <div class="warning">
            Код подставляется автоматически в формате <strong>ВБ-000000001</strong>. Ключ синхронизации справочников — <strong>code</strong>, документов — <strong>number</strong>.
        </div>
        <div class="section-label">Справочники</div>
        <div class="nav-list">
            <div class="nav-item active" data-page="page-clients" onclick="navigateTo('page-clients')"><div class="name">Клиенты</div></div>
            <div class="nav-item" data-page="page-employees" onclick="navigateTo('page-employees')"><div class="name">Сотрудники</div></div>
            <div class="nav-item" data-page="page-warehouses" onclick="navigateTo('page-warehouses')"><div class="name">Склады</div></div>
            <div class="nav-item" data-page="page-nomenclature" onclick="navigateTo('page-nomenclature')"><div class="name">Номенклатура</div></div>
        </div>
        <div class="section-label">Документы</div>
        <div class="nav-list">
            <div class="nav-item" data-page="page-goods" onclick="navigateTo('page-goods')"><div class="name">Приходная накладная</div></div>
            <div class="nav-item" data-page="page-services" onclick="navigateTo('page-services')"><div class="name">Оказание услуги</div></div>
        </div>
        <div class="section-label">Интеграция с 1С</div>
        <div class="nav-list">
            <div class="nav-item send1c" data-page="page-1c" onclick="navigateTo('page-1c')"><div class="name">Отправка в 1С</div></div>
        </div>

        <div id="page-clients" class="page active">
            <div class="card">
                <div class="card-header"><h2>Клиенты</h2><span class="badge">GET /api/clients · by-code /api/clients/by-code/{code}</span></div>
                <div class="hint" style="margin-bottom:8px;">Catalog.Клиенты: Код + Наименование (Ф.И.О., 100 символов).</div>
                <div id="clients-list"><div class="loading">Загрузка...</div></div>
                <div class="row-flex" style="margin-top:8px;">
                    <button class="btn btn-sm green" onclick="showTemplate('client')">Шаблоны (POST + by-code)</button>
                    <button class="btn btn-sm" onclick="toggleAddForm('add-client')">Добавить</button>
                </div>
                <div id="template-client" class="template-block"></div>
                <div id="add-client" class="add-form" style="display:none;">
                    <h4>Новый клиент</h4>
                    <div class="form-grid">
                        <label>Код</label>
                        <div class="code-row"><input id="new-client-code" maxlength="12" placeholder="ВБ-000000001" /><button class="btn btn-sm" title="Следующий свободный код" onclick="prefillNextCode('clients','new-client-code')">Обновить</button></div>
                        <label>Ф.И.О. *</label><input id="new-client-name" maxlength="100" placeholder="Иванов Иван Иванович" />
                    </div>
                    <div class="row-flex"><button class="btn green" onclick="createClient()">Сохранить</button><span id="new-client-status"></span></div>
                </div>
            </div>
        </div>

        <div id="page-employees" class="page">
            <div class="card">
                <div class="card-header"><h2>Сотрудники</h2><span class="badge">GET /api/employees · by-code /api/employees/by-code/{code}</span></div>
                <div class="hint" style="margin-bottom:8px;">Catalog.Сотрудники + ТЧ ТрудоваяДеятельность (Организация, НачалоРаботы, ОкончаниеРаботы, Должность).</div>
                <div id="employees-list"><div class="loading">Загрузка...</div></div>
                <div class="row-flex" style="margin-top:8px;">
                    <button class="btn btn-sm green" onclick="showTemplate('employee')">Шаблоны (POST + by-code)</button>
                    <button class="btn btn-sm" onclick="toggleAddForm('add-employee')">Добавить</button>
                </div>
                <div id="template-employee" class="template-block"></div>
                <div id="add-employee" class="add-form" style="display:none;">
                    <h4>Новый сотрудник</h4>
                    <div class="form-grid">
                        <label>Код</label>
                        <div class="code-row"><input id="new-emp-code" maxlength="12" placeholder="ВБ-000000001" /><button class="btn btn-sm" title="Следующий свободный код" onclick="prefillNextCode('employees','new-emp-code')">Обновить</button></div>
                        <label>Ф.И.О. *</label><input id="new-emp-name" maxlength="100" placeholder="Петров Петр Петрович" />
                    </div>
                    <h4 style="margin-top:10px;">Трудовая деятельность</h4>
                    <table class="tch-edit"><thead><tr><th>Организация</th><th style="width:130px;">Начало</th><th style="width:130px;">Окончание</th><th>Должность</th><th style="width:50px;"></th></tr></thead>
                    <tbody id="new-emp-rows"></tbody></table>
                    <div class="row-flex" style="margin-top:8px;"><button class="btn btn-sm" onclick="addEmpRow()">Строка</button></div>
                    <div class="row-flex" style="margin-top:8px;"><button class="btn green" onclick="createEmployee()">Сохранить</button><span id="new-emp-status"></span></div>
                </div>
            </div>
        </div>

        <div id="page-warehouses" class="page">
            <div class="card">
                <div class="card-header"><h2>Склады</h2><span class="badge">GET /api/warehouses · by-code /api/warehouses/by-code/{code}</span></div>
                <div class="hint" style="margin-bottom:8px;">Catalog.Склады: Код + Наименование (100 символов).</div>
                <div id="warehouses-list"><div class="loading">Загрузка...</div></div>
                <div class="row-flex" style="margin-top:8px;">
                    <button class="btn btn-sm green" onclick="showTemplate('warehouse')">Шаблоны (POST + by-code)</button>
                    <button class="btn btn-sm" onclick="toggleAddForm('add-wh')">Добавить</button>
                </div>
                <div id="template-warehouse" class="template-block"></div>
                <div id="add-wh" class="add-form" style="display:none;">
                    <h4>Новый склад</h4>
                    <div class="form-grid">
                        <label>Код</label>
                        <div class="code-row"><input id="new-wh-code" maxlength="12" placeholder="ВБ-000000001" /><button class="btn btn-sm" title="Следующий свободный код" onclick="prefillNextCode('warehouses','new-wh-code')">Обновить</button></div>
                        <label>Наименование *</label><input id="new-wh-name" maxlength="100" placeholder="Основной" />
                    </div>
                    <div class="row-flex"><button class="btn green" onclick="createWarehouse()">Сохранить</button><span id="new-wh-status"></span></div>
                </div>
            </div>
        </div>

        <div id="page-nomenclature" class="page">
            <div class="card">
                <div class="card-header"><h2>Номенклатура</h2><span class="badge">GET /api/nomenclature · by-code /api/nomenclature/by-code/{code}</span></div>
                <div class="hint" style="margin-bottom:8px;">Catalog.Номенклатура: иерархия (Группа номенклатуры + ЭтоГруппа) + ВидНоменклатуры (Материал/Услуга).</div>
                <div id="nomenclature-list"><div class="loading">Загрузка...</div></div>
                <div class="row-flex" style="margin-top:8px;">
                    <button class="btn btn-sm green" onclick="showTemplate('nomenclature')">Шаблоны (POST + by-code)</button>
                    <button class="btn btn-sm" onclick="toggleAddForm('add-nom');loadNomParents()">Добавить</button>
                </div>
                <div id="template-nomenclature" class="template-block"></div>
                <div id="add-nom" class="add-form" style="display:none;">
                    <h4>Новая номенклатура</h4>
                    <div class="form-grid">
                        <label>Код</label>
                        <div class="code-row"><input id="new-nom-code" maxlength="12" placeholder="ВБ-000000001" /><button class="btn btn-sm" title="Следующий свободный код" onclick="prefillNextCode('nomenclature','new-nom-code')">Обновить</button></div>
                        <label>Наименование *</label><input id="new-nom-name" maxlength="100" placeholder="Доска обрезная" />
                        <label>Это группа</label><select id="new-nom-folder"><option value="0">Нет (элемент)</option><option value="1">Да (группа)</option></select>
                        <label>Группа</label><select id="new-nom-parent"><option value="">— (корень)</option></select>
                        <label>Вид</label><select id="new-nom-vid"><option>Материал</option><option>Услуга</option></select>
                    </div>
                    <div class="row-flex"><button class="btn green" onclick="createNomenclature()">Сохранить</button><span id="new-nom-status"></span></div>
                </div>
            </div>
        </div>
        <div id="page-goods" class="page">
            <div class="card">
                <div class="card-header"><h2>Приходная накладная</h2><span class="badge">GET /api/goods_receipts · by-number /api/goods_receipts/by-number/{number}</span></div>
                <div class="hint" style="margin-bottom:8px;">Document.ПриходнаяНакладная: Номер + Дата + Склад + ТЧ Материалы (Материал, Количество 15.3, Цена 15.2, Сумма 15.2, НаборСвойств строкой).</div>
                <div class="row-flex">
                    <button class="btn btn-sm green" onclick="showTemplate('goods')">Шаблоны (POST + by-number)</button>
                    <button class="btn btn-sm" onclick="toggleAddForm('add-goods');loadGoodsRefs()">Добавить документ</button>
                </div>
                <div id="template-goods" class="template-block"></div>
                <div id="add-goods" class="add-form" style="display:none;">
                    <h4>Новая приходная накладная</h4>
                    <div class="form-grid">
                        <label>Номер *</label><input id="new-goods-number" maxlength="11" placeholder="ВБ-00000001" />
                        <label>Дата</label><input id="new-goods-date" type="date" />
                        <label>Склад</label><select id="new-goods-wh"><option value="">—</option></select>
                    </div>
                    <h4 style="margin-top:10px;">Табличная часть «Материалы»</h4>
                    <table class="tch-edit"><thead><tr><th>Материал</th><th style="width:100px;">Кол-во</th><th style="width:120px;">Цена</th><th style="width:130px;">Набор свойств</th><th style="width:50px;"></th></tr></thead>
                    <tbody id="new-goods-items"></tbody></table>
                    <div class="row-flex" style="margin-top:8px;">
                        <button class="btn btn-sm" onclick="addGoodsRow()">Строка</button>
                        <span class="hint" id="new-goods-total">Итого: 0</span>
                    </div>
                    <div class="row-flex" style="margin-top:8px;"><button class="btn green" onclick="createGoods()">Провести документ</button><span id="new-goods-status"></span></div>
                </div>
                <div id="goods-list" style="margin-top:10px;"><div class="loading">Загрузка...</div></div>
            </div>
        </div>

        <div id="page-services" class="page">
            <div class="card">
                <div class="card-header"><h2>Оказание услуги</h2><span class="badge">GET /api/service_docs · by-number /api/service_docs/by-number/{number}</span></div>
                <div class="hint" style="margin-bottom:8px;">Document.ОказаниеУслуги: Номер + Дата + Склад + Клиент + Мастер (Сотрудник) + ТЧ ПереченьНоменклатуры (Номенклатура, Количество, Цена, Сумма, НаборСвойств строкой).</div>
                <div class="row-flex">
                    <button class="btn btn-sm green" onclick="showTemplate('service')">Шаблоны (POST + by-number)</button>
                    <button class="btn btn-sm" onclick="toggleAddForm('add-service');loadServiceRefs()">Добавить документ</button>
                </div>
                <div id="template-service" class="template-block"></div>
                <div id="add-service" class="add-form" style="display:none;">
                    <h4>Новое оказание услуги</h4>
                    <div class="form-grid">
                        <label>Номер *</label><input id="new-svc-number" maxlength="11" placeholder="ВБ-00000001" />
                        <label>Дата</label><input id="new-svc-date" type="date" />
                        <label>Склад</label><select id="new-svc-wh"><option value="">—</option></select>
                        <label>Клиент</label><select id="new-svc-client"><option value="">—</option></select>
                        <label>Мастер</label><select id="new-svc-master"><option value="">—</option></select>
                    </div>
                    <h4 style="margin-top:10px;">Табличная часть «Перечень номенклатуры»</h4>
                    <table class="tch-edit"><thead><tr><th>Номенклатура</th><th style="width:100px;">Кол-во</th><th style="width:120px;">Цена</th><th style="width:130px;">Набор свойств</th><th style="width:50px;"></th></tr></thead>
                    <tbody id="new-svc-items"></tbody></table>
                    <div class="row-flex" style="margin-top:8px;">
                        <button class="btn btn-sm" onclick="addServiceRow()">Строка</button>
                        <span class="hint" id="new-svc-total">Итого: 0</span>
                    </div>
                    <div class="row-flex" style="margin-top:8px;"><button class="btn green" onclick="createService()">Провести документ</button><span id="new-svc-status"></span></div>
                </div>
                <div id="services-list" style="margin-top:10px;"><div class="loading">Загрузка...</div></div>
            </div>
        </div>

        <div id="page-1c" class="page">
            <div class="card">
                <div class="card-header"><h2>Подключение к 1С</h2><span class="badge">GET/PUT /api/settings_1c</span></div>
                <div class="form-grid">
                    <label>Base URL 1С</label><input id="s1c-base-url" placeholder="http://server/base/hs/api" />
                    <label>Таймаут (сек)</label><input id="s1c-timeout" type="number" min="1" max="120" />
                    <label>Проверять SSL</label><select id="s1c-verify"><option value="0">Нет</option><option value="1">Да</option></select>
                    <label>Авторизация</label><select id="s1c-use-auth" onchange="toggleAuth1c()"><option value="0">Без авторизации</option><option value="1">Логин + пароль (Basic)</option></select>
                    <label class="auth1c-row">Логин 1С</label><input class="auth1c-row" id="s1c-login" placeholder="Администратор" />
                    <label class="auth1c-row">Пароль 1С</label><input class="auth1c-row" id="s1c-password" type="password" placeholder="оставьте *** чтобы не менять" />
                    <label>Метод по умолчанию</label><select id="s1c-method"><option>POST</option><option>PUT</option><option>PATCH</option><option>GET</option><option>DELETE</option></select>
                </div>
                <div class="section-label">Пути отправки</div>
                <div class="form-grid">
                    <label>Клиенты</label><input id="s1c-ep-clients" placeholder="/clients" />
                    <label>Сотрудники</label><input id="s1c-ep-employees" placeholder="/employees" />
                    <label>Склады</label><input id="s1c-ep-warehouses" placeholder="/warehouses" />
                    <label>Номенклатура</label><input id="s1c-ep-nomenclature" placeholder="/nomenclature" />
                    <label>ПриходнаяНакладная</label><input id="s1c-ep-goods" placeholder="/goods_receipts" />
                    <label>ОказаниеУслуги</label><input id="s1c-ep-services" placeholder="/service_docs" />
                </div>
                <div class="row-flex">
                    <button class="btn green" onclick="saveSettings1c()">Сохранить</button>
                    <button class="btn" onclick="testConnection1c()">Проверить связь</button>
                    <span id="s1c-settings-status"></span>
                </div>
            </div>
            <div class="card">
                <div class="card-header"><h2>Отправка данных в 1С</h2><span class="badge">POST /api/send_to_1c</span></div>
                <div class="form-grid">
                    <label>Что отправляем</label>
                    <select id="send-entity" onchange="onEntityChange()">
                        <option value="clients">Клиент (из БД)</option>
                        <option value="employees">Сотрудник (из БД)</option>
                        <option value="warehouses">Склад (из БД)</option>
                        <option value="nomenclature">Номенклатура (из БД)</option>
                        <option value="goods_receipts">Приходная накладная (из БД)</option>
                        <option value="service_docs" selected>Оказание услуги (из БД)</option>
                        <option value="custom">Произвольный JSON</option>
                    </select>
                    <label>Запись из БД</label>
                    <div class="row-flex"><select id="send-record" style="flex:1;min-width:200px;"></select><button class="btn btn-sm" onclick="loadRecordsForSend()">Обновить</button></div>
                    <label>Метод</label>
                    <select id="send-method"><option>POST</option><option>PUT</option><option>PATCH</option><option>GET</option><option>DELETE</option></select>
                    <label>Путь отправки</label><input id="send-path" placeholder="/service_docs" />
                    <label>Тело запроса (JSON)</label><textarea id="send-body" rows="10" placeholder="{...}"></textarea>
                </div>
                <div class="row-flex">
                    <button class="btn" onclick="buildBodyPreview()">Подставить из БД</button>
                    <button class="btn green" onclick="sendTo1c()">Отправить в 1С</button>
                    <span id="send-status"></span>
                </div>
                <div class="hint" style="margin-top:8px;">Полный URL: <b>Base URL + Путь</b>. Всё сохраняется в журнале ниже.</div>
            </div>
            <div class="card">
                <div class="card-header"><h2>Журнал отправок в 1С</h2>
                    <div class="row-flex"><button class="btn btn-sm" onclick="loadLog()">Обновить</button><button class="btn btn-sm red" onclick="clearLog()">Очистить</button></div>
                </div>
                <div id="log-list"><div class="loading">Журнал пуст</div></div>
            </div>
        </div>
    </div>

    <div id="modal" class="modal">
        <div class="modal-content">
            <span class="close" onclick="closeModal()">&times;</span>
            <h3 id="modal-title">Просмотр JSON</h3>
            <div id="modal-body"></div>
        </div>
    </div>

    <script>
        function navigateTo(pageId) {
            document.querySelectorAll('.page').forEach(function(el){ el.classList.remove('active'); });
            document.querySelectorAll('.nav-item').forEach(function(el){ el.classList.remove('active'); });
            document.getElementById(pageId).classList.add('active');
            var nav = document.querySelector('.nav-item[data-page="' + pageId + '"]');
            if (nav) nav.classList.add('active');
            if (pageId === 'page-clients') loadClients();
            else if (pageId === 'page-employees') loadEmployees();
            else if (pageId === 'page-warehouses') loadWarehouses();
            else if (pageId === 'page-nomenclature') loadNomenclature();
            else if (pageId === 'page-goods') loadGoods();
            else if (pageId === 'page-services') loadServices();
            else if (pageId === 'page-1c') { loadSettings1c(); loadRecordsForSend(); loadLog(); }
        }
        function fetchData(url) {
            return fetch(url).then(function(r){ if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }).catch(function(e){ return { error: e.message }; });
        }
        function postJson(url, payload) {
            return fetch(url, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) }).then(function(r){
                return r.json().catch(function(){ return {}; }).then(function(d){ if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status)); return d; });
            });
        }
        function setStatus(id, ok, text) {
            document.getElementById(id).innerHTML = ok ? '<span class="ok-text">Сохранено: ' + text + '</span>' : '<span class="err-text">Ошибка: ' + text + '</span>';
        }
        function deleteItem(url, callback) {
            if (!confirm('Удалить запись?')) return;
            fetch(url, { method: 'DELETE' }).then(function(r){ return r.json().then(function(d){ if (!r.ok) throw new Error(d.error || 'Ошибка'); alert('Удалено'); if (callback) callback(); }); }).catch(function(e){ alert('Ошибка: ' + e.message); });
        }
        function toggleAddForm(id) {
            var el = document.getElementById(id);
            if (!el) return;
            var show = el.style.display === 'none';
            el.style.display = show ? 'block' : 'none';
            if (show) {
                if (id === 'add-client') prefillNextCode('clients', 'new-client-code');
                if (id === 'add-employee') { prefillNextCode('employees', 'new-emp-code'); if (!document.querySelectorAll('#new-emp-rows tr').length) addEmpRow(); }
                if (id === 'add-wh') prefillNextCode('warehouses', 'new-wh-code');
                if (id === 'add-nom') { prefillNextCode('nomenclature', 'new-nom-code'); loadNomParents(); }
                if (id === 'add-goods') { prefillNextNumber('goods_receipts', 'new-goods-number'); loadGoodsRefs(); if (!document.getElementById('new-goods-date').value) document.getElementById('new-goods-date').valueAsDate = new Date(); if (!document.querySelectorAll('#new-goods-items tr').length) addGoodsRow(); }
                if (id === 'add-service') { prefillNextNumber('service_docs', 'new-svc-number'); loadServiceRefs(); if (!document.getElementById('new-svc-date').value) document.getElementById('new-svc-date').valueAsDate = new Date(); if (!document.querySelectorAll('#new-svc-items tr').length) addServiceRow(); }
            }
        }
        function prefillNextCode(entity, inputId) {
            var inp = document.getElementById(inputId);
            if (!inp || inp.value) return;
            fetch('/api/next-code/' + entity).then(function(r){ return r.json(); }).then(function(d){ if (d.code) inp.value = d.code; }).catch(function(){});
        }
        function prefillNextNumber(entity, inputId) {
            var inp = document.getElementById(inputId);
            if (!inp || inp.value) return;
            fetch('/api/next-number/' + entity).then(function(r){ return r.json(); }).then(function(d){ if (d.number) inp.value = d.number; }).catch(function(){});
        }
        function renderTable(data, cols, apiBase, entity) {
            if (!data || data.error) return '<div class="status error">' + ((data && data.error) || 'Ошибка') + '</div>';
            if (!data.length) return '<div class="status empty">Нет записей</div>';
            var html = '<table><thead><tr>';
            cols.forEach(function(c){ html += '<th>' + c[1] + '</th>'; });
            html += '<th style="width:60px;">JSON</th><th style="width:60px;">В 1С</th><th style="width:60px;">Удалить</th></tr></thead><tbody>';
            data.forEach(function(item){
                html += '<tr>';
                cols.forEach(function(c){
                    var v = item[c[0]];
                    if (v === null || v === undefined) v = '';
                    if (typeof v === 'boolean') v = v ? 'Да' : 'Нет';
                    if (typeof v === 'string' && v.length > 40) v = v.substring(0,40)+'...';
                    html += '<td>' + v + '</td>';
                });
                html += '<td><button class="btn btn-sm" onclick="viewJson(\\'' + apiBase + '/' + item.id + '\\')">Просмотр</button></td>';
                html += '<td><button class="btn btn-sm green" title="Отправить в 1С" onclick="quickSendTo1c(\\'' + entity + '\\', ' + item.id + ')">В 1С</button></td>';
                html += '<td><button class="btn btn-sm red" onclick="deleteItem(\\'' + apiBase + '/' + item.id + '\\', function(){ navigateTo(document.querySelector(\\'.page.active\\').id); })">Удалить</button></td>';
                html += '</tr>';
            });
            return html + '</tbody></table>';
        }
        function loadClients() { fetchData('/api/clients').then(function(d){ document.getElementById('clients-list').innerHTML = renderTable(d, [['id','ID'],['code','Код'],['name','Ф.И.О.']], '/api/clients', 'clients'); }); }
        function loadWarehouses() { fetchData('/api/warehouses').then(function(d){ document.getElementById('warehouses-list').innerHTML = renderTable(d, [['id','ID'],['code','Код'],['name','Наименование']], '/api/warehouses', 'warehouses'); }); }
        function loadEmployees() {
            fetchData('/api/employees').then(function(d){
                if (!d || d.error) { document.getElementById('employees-list').innerHTML = '<div class="status error">' + ((d&&d.error)||'Ошибка') + '</div>'; return; }
                if (!d.length) { document.getElementById('employees-list').innerHTML = '<div class="status empty">Нет записей</div>'; return; }
                var html = '<table><thead><tr><th>ID</th><th>Код</th><th>Ф.И.О.</th><th>Строк ТЧ</th><th style="width:60px;">ТЧ</th><th style="width:60px;">JSON</th><th style="width:60px;">В 1С</th><th style="width:60px;">Удалить</th></tr></thead><tbody>';
                d.forEach(function(it){
                    var n = (it.employment||[]).length;
                    html += '<tr><td>' + it.id + '</td><td>' + (it.code||'') + '</td><td>' + (it.name||'') + '</td><td>' + n + '</td>';
                    html += '<td><button class="btn btn-sm orange" onclick="viewEmployee(\\'/api/employees/' + it.id + '\\')">ТЧ</button></td>';
                    html += '<td><button class="btn btn-sm" onclick="viewJson(\\'/api/employees/' + it.id + '\\')">Просмотр</button></td>';
                    html += '<td><button class="btn btn-sm green" onclick="quickSendTo1c(\\'employees\\',' + it.id + ')">В 1С</button></td>';
                    html += '<td><button class="btn btn-sm red" onclick="deleteItem(\\'/api/employees/' + it.id + '\\', loadEmployees)">Удалить</button></td></tr>';
                });
                document.getElementById('employees-list').innerHTML = html + '</tbody></table>';
            });
        }
        function loadNomenclature() {
            fetchData('/api/nomenclature').then(function(d){
                if (!d || d.error) { document.getElementById('nomenclature-list').innerHTML = '<div class="status error">' + ((d&&d.error)||'Ошибка') + '</div>'; return; }
                if (!d.length) { document.getElementById('nomenclature-list').innerHTML = '<div class="status empty">Нет записей</div>'; return; }
                var html = '<table><thead><tr><th>ID</th><th>Код</th><th>Наименование</th><th>Группа</th><th>ЭтоГруппа</th><th>Вид</th><th style="width:60px;">JSON</th><th style="width:60px;">В 1С</th><th style="width:60px;">Удалить</th></tr></thead><tbody>';
                d.forEach(function(it){
                    html += '<tr><td>' + it.id + '</td><td>' + (it.code||'') + '</td><td>' + (it.name||'') + '</td><td>' + (it.parent_name||'') + '</td><td>' + (it.is_folder?'Да':'Нет') + '</td><td>' + (it.vid||'') + '</td>';
                    html += '<td><button class="btn btn-sm" onclick="viewJson(\\'/api/nomenclature/' + it.id + '\\')">Просмотр</button></td>';
                    html += '<td><button class="btn btn-sm green" onclick="quickSendTo1c(\\'nomenclature\\',' + it.id + ')">В 1С</button></td>';
                    html += '<td><button class="btn btn-sm red" onclick="deleteItem(\\'/api/nomenclature/' + it.id + '\\', loadNomenclature)">Удалить</button></td></tr>';
                });
                document.getElementById('nomenclature-list').innerHTML = html + '</tbody></table>';
            });
        }
        function loadGoods() {
            fetchData('/api/goods_receipts').then(function(d){
                if (!d || d.error) { document.getElementById('goods-list').innerHTML = '<div class="status error">' + ((d&&d.error)||'Ошибка') + '</div>'; return; }
                if (!d.length) { document.getElementById('goods-list').innerHTML = '<div class="status empty">Нет документов</div>'; return; }
                var html = '<table><thead><tr><th>ID</th><th>Номер</th><th>Дата</th><th>Склад</th><th>Сумма</th><th style="width:60px;">JSON</th><th style="width:60px;">ТЧ</th><th style="width:60px;">В 1С</th><th style="width:60px;">Удалить</th></tr></thead><tbody>';
                d.forEach(function(doc){
                    html += '<tr><td>' + doc.id + '</td><td>' + doc.number + '</td><td>' + doc.doc_date + '</td><td>' + (doc.warehouse_name||'') + '</td><td>' + doc.total_sum + '</td>';
                    html += '<td><button class="btn btn-sm" onclick="viewJson(\\'/api/goods_receipts/' + doc.id + '\\')">Просмотр</button></td>';
                    html += '<td><button class="btn btn-sm orange" onclick="viewGoods(\\'/api/goods_receipts/' + doc.id + '\\')">ТЧ</button></td>';
                    html += '<td><button class="btn btn-sm green" onclick="quickSendTo1c(\\'goods_receipts\\',' + doc.id + ')">В 1С</button></td>';
                    html += '<td><button class="btn btn-sm red" onclick="deleteItem(\\'/api/goods_receipts/' + doc.id + '\\', loadGoods)">Удалить</button></td></tr>';
                });
                document.getElementById('goods-list').innerHTML = html + '</tbody></table>';
            });
        }
        function loadServices() {
            fetchData('/api/service_docs').then(function(d){
                if (!d || d.error) { document.getElementById('services-list').innerHTML = '<div class="status error">' + ((d&&d.error)||'Ошибка') + '</div>'; return; }
                if (!d.length) { document.getElementById('services-list').innerHTML = '<div class="status empty">Нет документов</div>'; return; }
                var html = '<table><thead><tr><th>ID</th><th>Номер</th><th>Дата</th><th>Склад</th><th>Клиент</th><th>Мастер</th><th>Сумма</th><th style="width:60px;">JSON</th><th style="width:60px;">ТЧ</th><th style="width:60px;">В 1С</th><th style="width:60px;">Удалить</th></tr></thead><tbody>';
                d.forEach(function(doc){
                    html += '<tr><td>' + doc.id + '</td><td>' + doc.number + '</td><td>' + doc.doc_date + '</td><td>' + (doc.warehouse_name||'') + '</td><td>' + (doc.client_name||'') + '</td><td>' + (doc.employee_name||'') + '</td><td>' + doc.total_sum + '</td>';
                    html += '<td><button class="btn btn-sm" onclick="viewJson(\\'/api/service_docs/' + doc.id + '\\')">Просмотр</button></td>';
                    html += '<td><button class="btn btn-sm orange" onclick="viewService(\\'/api/service_docs/' + doc.id + '\\')">ТЧ</button></td>';
                    html += '<td><button class="btn btn-sm green" onclick="quickSendTo1c(\\'service_docs\\',' + doc.id + ')">В 1С</button></td>';
                    html += '<td><button class="btn btn-sm red" onclick="deleteItem(\\'/api/service_docs/' + doc.id + '\\', loadServices)">Удалить</button></td></tr>';
                });
                document.getElementById('services-list').innerHTML = html + '</tbody></table>';
            });
        }
        function viewJson(url) {
            fetchData(url).then(function(d){
                if (d.error) { alert('Ошибка: ' + d.error); return; }
                document.getElementById('modal-title').textContent = url;
                document.getElementById('modal-body').innerHTML = '<div class="json-view">' + JSON.stringify(d, null, 2) + '</div>';
                document.getElementById('modal').style.display = 'flex';
                window._json = d;
            });
        }
        function viewEmployee(url) {
            fetchData(url).then(function(d){
                if (d.error) { alert('Ошибка: ' + d.error); return; }
                var rows = d.employment || [];
                var h = '<table class="items-table"><thead><tr><th>Организация</th><th>Начало</th><th>Окончание</th><th>Должность</th></tr></thead><tbody>';
                rows.forEach(function(r){ h += '<tr><td>' + (r.organization||'') + '</td><td>' + (r.start_date||'') + '</td><td>' + (r.end_date||'') + '</td><td>' + (r.position||'') + '</td></tr>'; });
                h += '</tbody></table>';
                if (!rows.length) h = '<div class="status empty">Нет строк в ТЧ</div>';
                document.getElementById('modal-title').textContent = 'Сотрудник: ' + d.name + ' (' + d.code + ')';
                document.getElementById('modal-body').innerHTML = '<div class="doc-detail-header"><div><strong>Код:</strong> ' + d.code + '</div><div><strong>Ф.И.О.:</strong> ' + d.name + '</div></div><h4>Трудовая деятельность</h4>' + h;
                document.getElementById('modal').style.display = 'flex';
            });
        }
        function viewGoods(url) {
            fetchData(url).then(function(d){
                if (d.error) { alert('Ошибка: ' + d.error); return; }
                var h = '<table class="items-table"><thead><tr><th>Материал</th><th>Кол-во</th><th>Цена</th><th>Сумма</th><th>Набор свойств</th></tr></thead><tbody>';
                (d.items||[]).forEach(function(it){ h += '<tr><td>' + (it.material_name||it.material_id) + '</td><td>' + it.quantity + '</td><td>' + it.price + '</td><td>' + it.sum + '</td><td>' + (it.variant||'') + '</td></tr>'; });
                h += '</tbody></table>';
                if (!(d.items||[]).length) h = '<div class="status empty">Нет строк в ТЧ</div>';
                document.getElementById('modal-title').textContent = 'Приходная накладная №' + d.number;
                document.getElementById('modal-body').innerHTML = '<div class="doc-detail-header"><div><strong>Номер:</strong> ' + d.number + '</div><div><strong>Дата:</strong> ' + d.doc_date + '</div><div><strong>Склад:</strong> ' + (d.warehouse_name||'') + '</div><div><strong>Сумма:</strong> ' + d.total_sum + '</div></div><h4>Материалы</h4>' + h;
                document.getElementById('modal').style.display = 'flex';
            });
        }
        function viewService(url) {
            fetchData(url).then(function(d){
                if (d.error) { alert('Ошибка: ' + d.error); return; }
                var h = '<table class="items-table"><thead><tr><th>Номенклатура</th><th>Кол-во</th><th>Цена</th><th>Сумма</th><th>Набор свойств</th></tr></thead><tbody>';
                (d.items||[]).forEach(function(it){ h += '<tr><td>' + (it.nomenclature_name||it.nomenclature_id) + '</td><td>' + it.quantity + '</td><td>' + it.price + '</td><td>' + it.sum + '</td><td>' + (it.variant||'') + '</td></tr>'; });
                h += '</tbody></table>';
                if (!(d.items||[]).length) h = '<div class="status empty">Нет строк в ТЧ</div>';
                document.getElementById('modal-title').textContent = 'Оказание услуги №' + d.number;
                document.getElementById('modal-body').innerHTML = '<div class="doc-detail-header"><div><strong>Номер:</strong> ' + d.number + '</div><div><strong>Дата:</strong> ' + d.doc_date + '</div><div><strong>Склад:</strong> ' + (d.warehouse_name||'') + '</div><div><strong>Клиент:</strong> ' + (d.client_name||'') + '</div><div><strong>Мастер:</strong> ' + (d.employee_name||'') + '</div><div><strong>Сумма:</strong> ' + d.total_sum + '</div></div><h4>Перечень номенклатуры</h4>' + h;
                document.getElementById('modal').style.display = 'flex';
            });
        }
        function closeModal() { document.getElementById('modal').style.display = 'none'; }
        document.getElementById('modal').addEventListener('click', function(e){ if (e.target === this) closeModal(); });
        var templates = {
            'client': { name: 'POST /api/clients | PUT/PATCH/DELETE /api/clients/by-code/{code}', json: JSON.stringify({code:'ВБ-000000001', name:'Иванов Иван Иванович'}, null, 2) },
            'employee': { name: 'POST /api/employees | PUT/PATCH/DELETE /api/employees/by-code/{code}', json: JSON.stringify({code:'ВБ-000000001', name:'Петров Петр Петрович', employment:[{organization:'ООО Ромашка', start_date:'2024-01-15', end_date:'2025-03-01', position:'Мастер'}]}, null, 2) },
            'warehouse': { name: 'POST /api/warehouses | PUT/PATCH/DELETE /api/warehouses/by-code/{code}', json: JSON.stringify({code:'ВБ-000000001', name:'Основной'}, null, 2) },
            'nomenclature': { name: 'POST /api/nomenclature | PUT/PATCH/DELETE /api/nomenclature/by-code/{code}', json: JSON.stringify({code:'ВБ-000000001', name:'Доска обрезная', parent_id:null, is_folder:false, vid:'Материал'}, null, 2) },
            'goods': { name: 'POST /api/goods_receipts | PUT/PATCH/DELETE /api/goods_receipts/by-number/{number}', json: JSON.stringify({number:'ВБ-00000001', warehouse_id:1, items:[{material_id:1, quantity:10, price:500, variant:''}]}, null, 2) },
            'service': { name: 'POST /api/service_docs | PUT/PATCH/DELETE /api/service_docs/by-number/{number}', json: JSON.stringify({number:'ВБ-00000001', warehouse_id:1, client_id:1, employee_id:1, items:[{nomenclature_id:1, quantity:2, price:1500, variant:''}]}, null, 2) }
        };
        function showTemplate(type) {
            var div = document.getElementById('template-' + type);
            if (div.style.display === 'block') { div.style.display = 'none'; return; }
            var t = templates[type];
            if (!t) return;
            div.innerHTML = '<strong>' + t.name + '</strong><pre>' + t.json + '</pre>';
            div.style.display = 'block';
        }
        function createClient() {
            postJson('/api/clients', { code: document.getElementById('new-client-code').value.trim(), name: document.getElementById('new-client-name').value.trim() }).then(function(d){
                setStatus('new-client-status', true, d.code + ' — ' + d.name);
                document.getElementById('new-client-name').value = ''; document.getElementById('new-client-code').value = '';
                prefillNextCode('clients', 'new-client-code'); loadClients();
            }).catch(function(e){ setStatus('new-client-status', false, e.message); });
        }
        function createWarehouse() {
            postJson('/api/warehouses', { code: document.getElementById('new-wh-code').value.trim(), name: document.getElementById('new-wh-name').value.trim() }).then(function(d){
                setStatus('new-wh-status', true, d.code + ' — ' + d.name);
                document.getElementById('new-wh-name').value = ''; document.getElementById('new-wh-code').value = '';
                prefillNextCode('warehouses', 'new-wh-code'); loadWarehouses();
            }).catch(function(e){ setStatus('new-wh-status', false, e.message); });
        }
        function addEmpRow() {
            var tr = document.createElement('tr');
            tr.innerHTML = '<td><input data-org maxlength="100" placeholder="Организация" /></td><td><input data-start type="date" /></td><td><input data-end type="date" /></td><td><input data-pos maxlength="100" placeholder="Должность" /></td><td><button class="btn btn-sm red" onclick="this.closest(\\'tr\\').remove()">X</button></td>';
            document.getElementById('new-emp-rows').appendChild(tr);
        }
        function createEmployee() {
            var rows = [];
            document.querySelectorAll('#new-emp-rows tr').forEach(function(tr){
                rows.push({ organization: tr.querySelector('input[data-org]').value, start_date: tr.querySelector('input[data-start]').value || null, end_date: tr.querySelector('input[data-end]').value || null, position: tr.querySelector('input[data-pos]').value });
            });
            postJson('/api/employees', { code: document.getElementById('new-emp-code').value.trim(), name: document.getElementById('new-emp-name').value.trim(), employment: rows }).then(function(d){
                setStatus('new-emp-status', true, d.code + ' — ' + d.name);
                document.getElementById('new-emp-name').value = ''; document.getElementById('new-emp-code').value = '';
                document.getElementById('new-emp-rows').innerHTML = ''; addEmpRow();
                prefillNextCode('employees', 'new-emp-code'); loadEmployees();
            }).catch(function(e){ setStatus('new-emp-status', false, e.message); });
        }
        var _nomList = [];
        function loadNomParents() {
            fetchData('/api/nomenclature').then(function(d){
                _nomList = (d && !d.error) ? d : [];
                var sel = document.getElementById('new-nom-parent');
                var folders = _nomList.filter(function(n){ return n.is_folder; });
                sel.innerHTML = '<option value="">— (корень)</option>' + folders.map(function(n){ return '<option value="' + n.id + '">[' + (n.code||'') + '] ' + n.name + '</option>'; }).join('');
            });
        }
        function createNomenclature() {
            var pid = document.getElementById('new-nom-parent').value;
            postJson('/api/nomenclature', { code: document.getElementById('new-nom-code').value.trim(), name: document.getElementById('new-nom-name').value.trim(), parent_id: pid ? parseInt(pid) : null, is_folder: document.getElementById('new-nom-folder').value === '1', vid: document.getElementById('new-nom-vid').value }).then(function(d){
                setStatus('new-nom-status', true, d.code + ' — ' + d.name);
                document.getElementById('new-nom-name').value = ''; document.getElementById('new-nom-code').value = '';
                prefillNextCode('nomenclature', 'new-nom-code'); loadNomenclature(); loadNomParents();
            }).catch(function(e){ setStatus('new-nom-status', false, e.message); });
        }
        var _goodsNoms = [];
        function loadGoodsRefs() {
            Promise.all([fetchData('/api/warehouses'), fetchData('/api/nomenclature')]).then(function(res){
                var whs = res[0], noms = res[1];
                if (whs && !whs.error) document.getElementById('new-goods-wh').innerHTML = '<option value="">—</option>' + whs.map(function(w){ return '<option value="' + w.id + '">[' + (w.code||'') + '] ' + w.name + '</option>'; }).join('');
                if (noms && !noms.error) { _goodsNoms = noms.filter(function(n){ return !n.is_folder; }); refreshGoodsNomSelects(); }
            });
        }
        function refreshGoodsNomSelects() {
            document.querySelectorAll('#new-goods-items select[data-nom]').forEach(function(sel){
                var cur = sel.value;
                sel.innerHTML = _goodsNoms.map(function(n){ return '<option value="' + n.id + '">[' + (n.code||'') + '] ' + n.name + '</option>'; }).join('');
                if (cur) sel.value = cur;
            });
        }
        function addGoodsRow() {
            var opts = _goodsNoms.map(function(n){ return '<option value="' + n.id + '">[' + (n.code||'') + '] ' + n.name + '</option>'; }).join('');
            var tr = document.createElement('tr');
            tr.innerHTML = '<td><select data-nom>' + opts + '</select></td><td><input type="number" data-qty value="1" min="0" step="0.001" oninput="recalcGoodsTotal()" /></td><td><input type="number" data-price value="0" min="0" step="0.01" oninput="recalcGoodsTotal()" /></td><td><input data-variant maxlength="25" placeholder="код варианта" /></td><td><button class="btn btn-sm red" onclick="this.closest(\\'tr\\').remove();recalcGoodsTotal()">X</button></td>';
            document.getElementById('new-goods-items').appendChild(tr);
            recalcGoodsTotal();
        }
        function recalcGoodsTotal() {
            var total = 0;
            document.querySelectorAll('#new-goods-items tr').forEach(function(tr){
                var q = parseFloat(tr.querySelector('input[data-qty]').value) || 0;
                var p = parseFloat(tr.querySelector('input[data-price]').value) || 0;
                total += q * p;
            });
            document.getElementById('new-goods-total').textContent = 'Итого: ' + total.toFixed(2);
        }
        function createGoods() {
            var rows = document.querySelectorAll('#new-goods-items tr');
            if (!rows.length) { setStatus('new-goods-status', false, 'Добавьте хотя бы одну строку'); return; }
            var items = [];
            rows.forEach(function(tr){ items.push({ material_id: parseInt(tr.querySelector('select[data-nom]').value), quantity: parseFloat(tr.querySelector('input[data-qty]').value) || 0, price: parseFloat(tr.querySelector('input[data-price]').value) || 0, variant: tr.querySelector('input[data-variant]').value }); });
            var wh = document.getElementById('new-goods-wh').value;
            postJson('/api/goods_receipts', { number: document.getElementById('new-goods-number').value.trim(), date: document.getElementById('new-goods-date').value || undefined, warehouse_id: wh ? parseInt(wh) : null, items: items }).then(function(d){
                setStatus('new-goods-status', true, 'Документ ' + d.number + ' на сумму ' + d.total_sum);
                document.getElementById('new-goods-number').value = ''; document.getElementById('new-goods-items').innerHTML = '';
                addGoodsRow(); loadGoods();
            }).catch(function(e){ setStatus('new-goods-status', false, e.message); });
        }
        var _svcNoms = [];
        function loadServiceRefs() {
            Promise.all([fetchData('/api/warehouses'), fetchData('/api/clients'), fetchData('/api/employees'), fetchData('/api/nomenclature')]).then(function(res){
                var whs = res[0], cls = res[1], emps = res[2], noms = res[3];
                if (whs && !whs.error) document.getElementById('new-svc-wh').innerHTML = '<option value="">—</option>' + whs.map(function(w){ return '<option value="' + w.id + '">[' + (w.code||'') + '] ' + w.name + '</option>'; }).join('');
                if (cls && !cls.error) document.getElementById('new-svc-client').innerHTML = '<option value="">—</option>' + cls.map(function(c){ return '<option value="' + c.id + '">[' + (c.code||'') + '] ' + c.name + '</option>'; }).join('');
                if (emps && !emps.error) document.getElementById('new-svc-master').innerHTML = '<option value="">—</option>' + emps.map(function(e){ return '<option value="' + e.id + '">[' + (e.code||'') + '] ' + e.name + '</option>'; }).join('');
                if (noms && !noms.error) { _svcNoms = noms.filter(function(n){ return !n.is_folder; }); refreshSvcNomSelects(); }
            });
        }
        function refreshSvcNomSelects() {
            document.querySelectorAll('#new-svc-items select[data-nom]').forEach(function(sel){
                var cur = sel.value;
                sel.innerHTML = _svcNoms.map(function(n){ return '<option value="' + n.id + '">[' + (n.code||'') + '] ' + n.name + '</option>'; }).join('');
                if (cur) sel.value = cur;
            });
        }
        function addServiceRow() {
            var opts = _svcNoms.map(function(n){ return '<option value="' + n.id + '">[' + (n.code||'') + '] ' + n.name + '</option>'; }).join('');
            var tr = document.createElement('tr');
            tr.innerHTML = '<td><select data-nom>' + opts + '</select></td><td><input type="number" data-qty value="1" min="0" step="0.001" oninput="recalcSvcTotal()" /></td><td><input type="number" data-price value="0" min="0" step="0.01" oninput="recalcSvcTotal()" /></td><td><input data-variant maxlength="25" placeholder="код варианта" /></td><td><button class="btn btn-sm red" onclick="this.closest(\\'tr\\').remove();recalcSvcTotal()">X</button></td>';
            document.getElementById('new-svc-items').appendChild(tr);
            recalcSvcTotal();
        }
        function recalcSvcTotal() {
            var total = 0;
            document.querySelectorAll('#new-svc-items tr').forEach(function(tr){
                var q = parseFloat(tr.querySelector('input[data-qty]').value) || 0;
                var p = parseFloat(tr.querySelector('input[data-price]').value) || 0;
                total += q * p;
            });
            document.getElementById('new-svc-total').textContent = 'Итого: ' + total.toFixed(2);
        }
        function createService() {
            var rows = document.querySelectorAll('#new-svc-items tr');
            if (!rows.length) { setStatus('new-svc-status', false, 'Добавьте хотя бы одну строку'); return; }
            var items = [];
            rows.forEach(function(tr){ items.push({ nomenclature_id: parseInt(tr.querySelector('select[data-nom]').value), quantity: parseFloat(tr.querySelector('input[data-qty]').value) || 0, price: parseFloat(tr.querySelector('input[data-price]').value) || 0, variant: tr.querySelector('input[data-variant]').value }); });
            var wh = document.getElementById('new-svc-wh').value, cl = document.getElementById('new-svc-client').value, ms = document.getElementById('new-svc-master').value;
            postJson('/api/service_docs', { number: document.getElementById('new-svc-number').value.trim(), date: document.getElementById('new-svc-date').value || undefined, warehouse_id: wh ? parseInt(wh) : null, client_id: cl ? parseInt(cl) : null, employee_id: ms ? parseInt(ms) : null, items: items }).then(function(d){
                setStatus('new-svc-status', true, 'Документ ' + d.number + ' на сумму ' + d.total_sum);
                document.getElementById('new-svc-number').value = ''; document.getElementById('new-svc-items').innerHTML = '';
                addServiceRow(); loadServices();
            }).catch(function(e){ setStatus('new-svc-status', false, e.message); });
        }
        var _s1c = null;
        function toggleAuth1c() {
            var use = document.getElementById('s1c-use-auth').value === '1';
            document.querySelectorAll('.auth1c-row').forEach(function(el){ el.style.display = use ? '' : 'none'; });
        }
        function loadSettings1c() {
            fetchData('/api/settings_1c').then(function(d){
                if (d.error) return;
                _s1c = d;
                document.getElementById('s1c-base-url').value = d.base_url || '';
                document.getElementById('s1c-timeout').value = d.timeout || 10;
                document.getElementById('s1c-verify').value = d.verify_ssl ? '1' : '0';
                document.getElementById('s1c-use-auth').value = d.use_auth ? '1' : '0';
                document.getElementById('s1c-login').value = d.login || '';
                document.getElementById('s1c-password').value = '';
                document.getElementById('s1c-password').placeholder = d.has_password ? '*** (сохранён)' : 'пароль не задан';
                document.getElementById('s1c-method').value = d.default_method || 'POST';
                var ep = d.endpoints || {};
                document.getElementById('s1c-ep-clients').value = ep.clients || '';
                document.getElementById('s1c-ep-employees').value = ep.employees || '';
                document.getElementById('s1c-ep-warehouses').value = ep.warehouses || '';
                document.getElementById('s1c-ep-nomenclature').value = ep.nomenclature || '';
                document.getElementById('s1c-ep-goods').value = ep.goods_receipts || '';
                document.getElementById('s1c-ep-services').value = ep.service_docs || '';
                toggleAuth1c();
                if (!document.getElementById('send-path').value) onEntityChange();
            });
        }
        function saveSettings1c() {
            var st = document.getElementById('s1c-settings-status');
            st.textContent = 'Сохранение...';
            var payload = { base_url: document.getElementById('s1c-base-url').value.trim(), timeout: parseInt(document.getElementById('s1c-timeout').value) || 10, verify_ssl: document.getElementById('s1c-verify').value === '1', use_auth: document.getElementById('s1c-use-auth').value === '1', login: document.getElementById('s1c-login').value.trim(), default_method: document.getElementById('s1c-method').value,
                endpoints: { clients: document.getElementById('s1c-ep-clients').value.trim(), employees: document.getElementById('s1c-ep-employees').value.trim(), warehouses: document.getElementById('s1c-ep-warehouses').value.trim(), nomenclature: document.getElementById('s1c-ep-nomenclature').value.trim(), goods_receipts: document.getElementById('s1c-ep-goods').value.trim(), service_docs: document.getElementById('s1c-ep-services').value.trim() } };
            var pwd = document.getElementById('s1c-password').value;
            if (pwd && pwd !== '***') payload.password = pwd;
            fetch('/api/settings_1c', { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) }).then(function(r){ return r.json().then(function(d){ if (!r.ok) throw new Error(d.error || 'Ошибка'); _s1c = d; st.innerHTML = '<span class="ok-text">Сохранено</span>'; onEntityChange(); }); }).catch(function(e){ st.innerHTML = '<span class="err-text">Ошибка: ' + e.message + '</span>'; });
        }
        function testConnection1c() {
            var st = document.getElementById('s1c-settings-status');
            st.textContent = 'Проверка...';
            fetch('/api/test_1c', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ base_url: document.getElementById('s1c-base-url').value.trim() }) }).then(function(r){ return r.json(); }).then(function(d){
                st.innerHTML = d.success ? '<span class="ok-text">Связь есть (HTTP ' + d.status + ')</span>' : '<span class="err-text">Нет связи: ' + (d.error || ('HTTP ' + d.status)) + '</span>';
            }).catch(function(e){ st.innerHTML = '<span class="err-text">Ошибка: ' + e.message + '</span>'; });
        }
        var ENTITY_API = { clients: '/api/clients', employees: '/api/employees', warehouses: '/api/warehouses', nomenclature: '/api/nomenclature', goods_receipts: '/api/goods_receipts', service_docs: '/api/service_docs' };
        function onEntityChange() {
            var ent = document.getElementById('send-entity').value;
            var isCustom = (ent === 'custom');
            document.getElementById('send-record').disabled = isCustom;
            if (_s1c && _s1c.endpoints && _s1c.endpoints[ent]) document.getElementById('send-path').value = _s1c.endpoints[ent];
            if (_s1c && _s1c.default_method) document.getElementById('send-method').value = _s1c.default_method;
            if (isCustom) { document.getElementById('send-path').value = document.getElementById('send-path').value || '/custom'; }
            else loadRecordsForSend();
        }
        function loadRecordsForSend() {
            var ent = document.getElementById('send-entity').value;
            var sel = document.getElementById('send-record');
            if (ent === 'custom') { sel.innerHTML = '<option value="">—</option>'; return; }
            fetchData(ENTITY_API[ent]).then(function(d){
                if (!d || d.error || !d.length) { sel.innerHTML = '<option value="">(нет записей)</option>'; return; }
                sel.innerHTML = d.map(function(it){
                    var label = '#' + it.id;
                    if (it.code) label += ' [' + it.code + ']';
                    if (it.name) label += ' — ' + it.name;
                    else if (it.number) label += ' — ' + it.number;
                    return '<option value="' + it.id + '">' + label + '</option>';
                }).join('');
                buildBodyPreview();
            });
        }
        function buildBodyPreview() {
            var ent = document.getElementById('send-entity').value;
            if (ent === 'custom') return;
            var rid = document.getElementById('send-record').value;
            if (!rid) { document.getElementById('send-body').value = ''; return; }
            fetchData('/api/build_1c_body?entity=' + ent + '&record_id=' + rid).then(function(d){
                if (d.error) { alert('Ошибка: ' + d.error); return; }
                document.getElementById('send-body').value = JSON.stringify(d, null, 2);
            });
        }
        function sendTo1c() {
            var st = document.getElementById('send-status');
            var ent = document.getElementById('send-entity').value;
            var method = document.getElementById('send-method').value;
            var path = document.getElementById('send-path').value.trim();
            var raw = document.getElementById('send-body').value.trim();
            var body = null;
            if (raw && method !== 'GET' && method !== 'DELETE') {
                try { body = JSON.parse(raw); }
                catch(e) { st.innerHTML = '<span class="err-text">Тело — невалидный JSON: ' + e.message + '</span>'; return; }
            }
            var payload = { entity: ent, method: method, path: path, body: body };
            if (ent !== 'custom') payload.record_id = parseInt(document.getElementById('send-record').value) || null;
            st.textContent = 'Отправка...';
            fetch('/api/send_to_1c', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) }).then(function(r){ return r.json().then(function(d){
                st.innerHTML = d.success ? '<span class="ok-text">Отправлено: ' + d.method + ' ' + d.url + ' (HTTP ' + d.status + ')</span>' : '<span class="err-text">Ошибка (HTTP ' + (d.status === null || d.status === undefined ? '—' : d.status) + '): ' + (d.error || ('' + (d.response||'')).substring(0,300)) + '</span>';
                loadLog();
            }); }).catch(function(e){ st.innerHTML = '<span class="err-text">Ошибка: ' + e.message + '</span>'; });
        }
        function quickSendTo1c(entity, id) {
            if (!confirm('Отправить запись #' + id + ' (' + entity + ') в 1С?')) return;
            fetchData('/api/settings_1c').then(function(s){ if (!s.error) _s1c = s; });
            var path = (_s1c && _s1c.endpoints && _s1c.endpoints[entity]) || ('/' + entity);
            var method = (_s1c && _s1c.default_method) || 'POST';
            fetch('/api/send_to_1c', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ entity: entity, record_id: id, method: method, path: path }) }).then(function(r){ return r.json(); }).then(function(d){
                alert(d.success ? 'Отправлено в 1С: ' + d.method + ' ' + d.url + ' (HTTP ' + d.status + ')' : 'Ошибка отправки: ' + (d.error || d.response || ('HTTP ' + d.status)));
            }).catch(function(e){ alert('Ошибка: ' + e.message); });
        }
        function loadLog() {
            var box = document.getElementById('log-list');
            if (!box) return;
            fetchData('/api/outbox_log?limit=100').then(function(d){
                if (!d || d.error) { box.innerHTML = '<div class="status error">' + ((d&&d.error)||'Ошибка') + '</div>'; return; }
                if (!d.length) { box.innerHTML = '<div class="status empty">Пока ничего не отправляли</div>'; return; }
                var html = '<table><thead><tr><th>ID</th><th>Время</th><th>Сущность</th><th>Метод</th><th>URL</th><th>Статус</th><th></th><th></th></tr></thead><tbody>';
                d.forEach(function(l){
                    var tag = l.success ? '<span class="tag-ok">OK</span>' : '<span class="tag-fail">FAIL</span>';
                    var shortUrl = (l.url || '').length > 45 ? (l.url.substring(0,45) + '...') : (l.url || '');
                    html += '<tr><td>' + l.id + '</td><td>' + (l.created_at||'').substring(0,19).replace('T',' ') + '</td><td>' + (l.entity||'') + ' ' + (l.record_id === null || l.record_id === undefined ? '' : l.record_id) + '</td><td>' + (l.method||'') + '</td><td title="' + (l.url||'') + '">' + shortUrl + '</td><td>' + (l.response_status === null || l.response_status === undefined ? '—' : l.response_status) + ' ' + tag + '</td><td><button class="btn btn-sm" onclick="viewLogDetail(' + l.id + ')">Просмотр</button></td><td><button class="btn btn-sm red" onclick="deleteLogItem(' + l.id + ')">Удалить</button></td></tr>';
                });
                box.innerHTML = html + '</tbody></table>';
                window._logCache = {};
                d.forEach(function(l){ window._logCache[l.id] = l; });
            });
        }
        function viewLogDetail(id) {
            var l = window._logCache && window._logCache[id];
            if (!l) return;
            document.getElementById('modal-title').textContent = 'Отправка #' + l.id;
            document.getElementById('modal-body').innerHTML = '<div class="doc-detail-header"><div><strong>Время:</strong> ' + (l.created_at||'').substring(0,19).replace('T',' ') + '</div><div><strong>Метод:</strong> ' + l.method + '</div><div><strong>HTTP:</strong> ' + (l.response_status === null || l.response_status === undefined ? '—' : l.response_status) + '</div></div><h4>Запрос</h4><div class="json-view">' + ((l.request_body||'(пусто)').substring(0,4000)) + '</div><h4 style="margin-top:10px;">Ответ 1С</h4><div class="json-view">' + ((l.response_body||'(пусто)').substring(0,4000)) + '</div>';
            document.getElementById('modal').style.display = 'flex';
        }
        function deleteLogItem(id) {
            if (!confirm('Удалить запись журнала?')) return;
            fetch('/api/outbox_log/' + id, { method: 'DELETE' }).then(function(){ loadLog(); });
        }
        function clearLog() {
            if (!confirm('Очистить весь журнал отправок?')) return;
            fetch('/api/outbox_log', { method: 'DELETE' }).then(function(){ loadLog(); });
        }
        window.onload = function() { loadClients(); loadSettings1c(); };
    </script>
</body>
</html>
''')

# ---------- УДАЛЕНИЕ ТАБЛИЦ СТАРОЙ КОНФИГУРАЦИИ ----------
# Организации / Контрагенты / Единицы / старая Номенклатура / Реализация
# больше не используются — удаляем их таблицы при старте.
LEGACY_TABLES = ('sales_document_items', 'sales_documents',
                 'organizations', 'counterparties', 'units', 'nomenclatures')

def drop_legacy_tables():
    try:
        with db.engine.begin() as conn:
            for tbl in LEGACY_TABLES:
                conn.execute(db.text(f"DROP TABLE IF EXISTS {tbl} CASCADE"))
    except Exception as e:
        print('drop_legacy_tables warning:', e)

# ---------- СОЗДАНИЕ ТАБЛИЦ + МИГРАЦИЯ ----------
with app.app_context():
    db.create_all()
    drop_legacy_tables()
    ensure_code_columns()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)



