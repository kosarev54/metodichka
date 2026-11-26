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
# ↓↓↓ ПОДСТАВЬ СВОИ ДАННЫЕ ДЛЯ POSTGRESQL ↓↓↓
# ==============================================
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://postgres:1234@localhost:5432/WebService'
# ==============================================

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
ma = Marshmallow(app)

# ============================================================
# НАСТРОЙКИ ОТПРАВКИ В 1С (хранятся в 1c_settings.json рядом с app.py)
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_1C_FILE = os.path.join(BASE_DIR, '1c_settings.json')

DEFAULT_1C_SETTINGS = {
    # Пример: "base_url": "http://192.168.1.10/MyBase/hs/api"
    "base_url": "http://192.168.1.10/MyBase/hs/api",
    "timeout": 10,
    "verify_ssl": False,
    "use_auth": False,
    "login": "",
    "password": "",
    "default_method": "POST",
    # Пути отправки для каждого типа данных (дописываются к base_url).
    # Можно менять прямо в интерфейсе без правки кода.
    "endpoints": {
        "organizations": "/organizations",
        "counterparties": "/counterparties",
        "units": "/units",
        "nomenclatures": "/nomenclatures",
        "sales": "/sales"
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
            # endpoints и headers мержим отдельно, чтобы не потерять ключи по умолчанию
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
    # пароль не светим в ответе, но храним если прислали непустой
    with open(SETTINGS_1C_FILE, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    return settings

def public_1c_settings():
    """Настройки для отдачи в UI (пароль скрыт)."""
    s = load_1c_settings()
    s = dict(s)
    if s.get('password'):
        s['password'] = '***'
        s['has_password'] = True
    else:
        s['has_password'] = False
    return s

def do_http_to_1c(url, method, body_obj, settings, extra_headers=None):
    """Отправка HTTP-запроса в 1С через urllib (без зависимости requests)."""
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
    # Basic-авторизация — только если включена в настройках
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

def build_1c_body(entity, record_id):
    """Собрать JSON-тело для отправки в 1С из записи в нашей БД."""
    if entity == 'organizations':
        o = Organization.query.get(record_id)
        if not o:
            return None, 'Организация не найдена'
        return {'code': o.code or '', 'name': o.name, 'inn': o.inn, 'kpp': o.kpp}, None
    if entity == 'counterparties':
        c = Counterparty.query.get(record_id)
        if not c:
            return None, 'Контрагент не найден'
        return {'code': c.code or '', 'name': c.name, 'inn': c.inn, 'type': c.type}, None
    if entity == 'units':
        u = Unit.query.get(record_id)
        if not u:
            return None, 'Единица не найдена'
        return {'code': u.code or '', 'name': u.name}, None
    if entity == 'nomenclatures':
        n = Nomenclature.query.get(record_id)
        if not n:
            return None, 'Номенклатура не найдена'
        return {'code': n.code or '', 'name': n.name, 'unit_id': n.unit_id, 'price': float(n.price or 0)}, None
    if entity == 'sales':
        d = SalesDocument.query.get(record_id)
        if not d:
            return None, 'Документ не найден'
        items = []
        for it in d.items:
            items.append({
                'nomenclature_id': it.nomenclature_id,
                'quantity': float(it.quantity or 0),
                'price': float(it.price or 0)
            })
        return {
            'number': d.number,
            'date': d.doc_date.isoformat() if d.doc_date else None,
            'organization_id': d.organization_id,
            'counterparty_id': d.counterparty_id,
            'comment': d.comment or '',
            'items': items
        }, None
    return None, 'Неизвестный тип: ' + str(entity)

# ---------- МОДЕЛИ ----------
# code — как в 1С (Код справочника), нужен для будущей синхронизации.
# Стандартный код — вида ВБ-000000001 (префикс ВБ- + 9 цифр = 12 символов).
# number у документа — номер документа 1С (11 символов), по нему тоже идёт адресация.
CODE_PREFIX = 'ВБ-'
CODE_DIGITS = 9
CODE_LEN = len(CODE_PREFIX) + CODE_DIGITS  # 12
NUMBER_LEN = 11

class Organization(db.Model):
    __tablename__ = 'organizations'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(CODE_LEN), default='')
    name = db.Column(db.String(200), nullable=False)
    inn = db.Column(db.String(12))
    kpp = db.Column(db.String(9))

class Counterparty(db.Model):
    __tablename__ = 'counterparties'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(CODE_LEN), default='')
    name = db.Column(db.String(200), nullable=False)
    inn = db.Column(db.String(12))
    type = db.Column(db.String(20), default='ЮрЛицо')

class Unit(db.Model):
    __tablename__ = 'units'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    code = db.Column(db.String(CODE_LEN), default='')

class Nomenclature(db.Model):
    __tablename__ = 'nomenclatures'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(CODE_LEN), default='')
    name = db.Column(db.String(200), nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey('units.id'))
    price = db.Column(db.Numeric(15,2), default=0)
    unit = db.relationship('Unit')

def ensure_code_columns():
    """Миграция для существующих БД: добавляет code и приводит длину code/number."""
    try:
        with db.engine.begin() as conn:
            conn.execute(db.text(f"ALTER TABLE organizations ADD COLUMN IF NOT EXISTS code VARCHAR({CODE_LEN}) DEFAULT ''"))
            conn.execute(db.text(f"ALTER TABLE counterparties ADD COLUMN IF NOT EXISTS code VARCHAR({CODE_LEN}) DEFAULT ''"))
            conn.execute(db.text(f"ALTER TABLE units ADD COLUMN IF NOT EXISTS code VARCHAR({CODE_LEN}) DEFAULT ''"))
            conn.execute(db.text(f"ALTER TABLE nomenclatures ADD COLUMN IF NOT EXISTS code VARCHAR({CODE_LEN}) DEFAULT ''"))
            # Приводим старые колонки к актуальной длине, обрезая слишком длинные
            for tbl in ('organizations', 'counterparties', 'units', 'nomenclatures'):
                conn.execute(db.text(f"UPDATE {tbl} SET code = LEFT(code, {CODE_LEN}) WHERE LENGTH(code) > {CODE_LEN}"))
                conn.execute(db.text(f"ALTER TABLE {tbl} ALTER COLUMN code TYPE VARCHAR({CODE_LEN})"))
            conn.execute(db.text(f"UPDATE sales_documents SET number = LEFT(number, {NUMBER_LEN}) WHERE LENGTH(number) > {NUMBER_LEN}"))
            conn.execute(db.text(f"ALTER TABLE sales_documents ALTER COLUMN number TYPE VARCHAR({NUMBER_LEN})"))
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
    """Проверка уникальности кода (пустой код — не проверяем)."""
    code = (code or '').strip()
    if not code:
        return False
    q = model.query.filter(model.code == code)
    if exclude_id is not None:
        q = q.filter(model.id != exclude_id)
    return q.first() is not None

def generate_next_code(model):
    """Следующий свободный код вида ВБ-000000001 (префикс ВБ- + 9 цифр)."""
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

# ---------- ДОКУМЕНТ: РЕАЛИЗАЦИЯ ----------
class SalesDocument(db.Model):
    __tablename__ = 'sales_documents'
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(NUMBER_LEN), nullable=False)
    doc_date = db.Column(db.Date, default=date.today)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'))
    counterparty_id = db.Column(db.Integer, db.ForeignKey('counterparties.id'))
    total_sum = db.Column(db.Numeric(15,2), default=0)
    comment = db.Column(db.Text)
    
    organization = db.relationship('Organization')
    counterparty = db.relationship('Counterparty')
    items = db.relationship('SalesDocumentItem', cascade='all, delete-orphan', backref='document')

class SalesDocumentItem(db.Model):
    __tablename__ = 'sales_document_items'
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey('sales_documents.id', ondelete='CASCADE'))
    nomenclature_id = db.Column(db.Integer, db.ForeignKey('nomenclatures.id'))
    quantity = db.Column(db.Numeric(15,3), default=0)
    price = db.Column(db.Numeric(15,2), default=0)
    sum = db.Column(db.Numeric(15,2), default=0)
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

# ---------- СХЕМЫ ----------
class OrganizationSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = Organization

class CounterpartySchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = Counterparty

class UnitSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = Unit

class NomenclatureSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = Nomenclature
        include_fk = True
    unit_name = ma.Method('get_unit_name')
    def get_unit_name(self, obj):
        return obj.unit.name if obj.unit else None

class SalesDocumentItemSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = SalesDocumentItem
        include_fk = True
    nomenclature_name = ma.Method('get_nomenclature_name')
    def get_nomenclature_name(self, obj):
        return obj.nomenclature.name if obj.nomenclature else None

class SalesDocumentSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = SalesDocument
        include_fk = True
    items = ma.Nested(SalesDocumentItemSchema, many=True)
    organization_name = ma.Method('get_organization_name')
    counterparty_name = ma.Method('get_counterparty_name')
    def get_organization_name(self, obj):
        return obj.organization.name if obj.organization else None
    def get_counterparty_name(self, obj):
        return obj.counterparty.name if obj.counterparty else None

class SendLogSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = SendLog


# ============================================================
# ОРГАНИЗАЦИИ
# ============================================================
@app.route('/api/organizations', methods=['GET'])
def get_organizations():
    return jsonify(OrganizationSchema(many=True).dump(Organization.query.all()))

@app.route('/api/organizations/<int:id>', methods=['GET'])
def get_organization(id):
    return jsonify(OrganizationSchema().dump(Organization.query.get_or_404(id)))

@app.route('/api/organizations', methods=['POST'])
def add_organization():
    data = request.get_json(force=True, silent=True) or {}
    try:
        if not data.get('name'):
            return jsonify({'error': 'Поле name обязательно'}), 400
        code = (data.get('code') or '').strip()
        if not code:
            code = generate_next_code(Organization)
        err = _check_code_len(code)
        if err:
            return jsonify({'error': err}), 400
        if code and _code_taken(Organization, code):
            return jsonify({'error': f'Код {code} уже занят'}), 400
        item = Organization(code=code, name=data['name'], inn=data.get('inn'), kpp=data.get('kpp'))
        db.session.add(item)
        db.session.commit()
        return jsonify(OrganizationSchema().dump(item)), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

def _apply_org_fields(item, data, exclude_id=None):
    if 'code' in data:
        code = (data.get('code') or '').strip()
        err = _check_code_len(code)
        if err:
            return err
        if code and _code_taken(Organization, code, exclude_id):
            return f'Код {code} уже занят'
        item.code = code
    if 'name' in data: item.name = data['name']
    if 'inn' in data: item.inn = data['inn']
    if 'kpp' in data: item.kpp = data['kpp']
    return None

@app.route('/api/organizations/<int:id>', methods=['PUT', 'PATCH'])
def update_organization(id):
    item = Organization.query.get_or_404(id)
    data = request.get_json(force=True, silent=True) or {}
    try:
        err = _apply_org_fields(item, data, exclude_id=id)
        if err:
            return jsonify({'error': err}), 400
        db.session.commit()
        return jsonify(OrganizationSchema().dump(item))
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/organizations/<int:id>', methods=['DELETE'])
def delete_organization(id):
    item = Organization.query.get_or_404(id)
    try:
        db.session.delete(item)
        db.session.commit()
        return jsonify({'message': 'Организация удалена', 'id': id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

# --- Работа по коду (для синхронизации с 1С) ---
@app.route('/api/organizations/by-code/<code>', methods=['GET'])
def get_organization_by_code(code):
    item = Organization.query.filter_by(code=code).first_or_404()
    return jsonify(OrganizationSchema().dump(item))

@app.route('/api/organizations/by-code/<code>', methods=['PUT', 'PATCH'])
def update_organization_by_code(code):
    item = Organization.query.filter_by(code=code).first_or_404()
    data = request.get_json(force=True, silent=True) or {}
    try:
        err = _apply_org_fields(item, data, exclude_id=item.id)
        if err:
            return jsonify({'error': err}), 400
        db.session.commit()
        return jsonify(OrganizationSchema().dump(item))
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/organizations/by-code/<code>', methods=['DELETE'])
def delete_organization_by_code(code):
    item = Organization.query.filter_by(code=code).first_or_404()
    try:
        db.session.delete(item)
        db.session.commit()
        return jsonify({'message': 'Организация удалена', 'code': code})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400


# ============================================================
# КОНТРАГЕНТЫ
# ============================================================
@app.route('/api/counterparties', methods=['GET'])
def get_counterparties():
    return jsonify(CounterpartySchema(many=True).dump(Counterparty.query.all()))

@app.route('/api/counterparties/<int:id>', methods=['GET'])
def get_counterparty(id):
    return jsonify(CounterpartySchema().dump(Counterparty.query.get_or_404(id)))

@app.route('/api/counterparties', methods=['POST'])
def add_counterparty():
    data = request.get_json(force=True, silent=True) or {}
    try:
        if not data.get('name'):
            return jsonify({'error': 'Поле name обязательно'}), 400
        code = (data.get('code') or '').strip()
        if not code:
            code = generate_next_code(Counterparty)
        err = _check_code_len(code)
        if err:
            return jsonify({'error': err}), 400
        if code and _code_taken(Counterparty, code):
            return jsonify({'error': f'Код {code} уже занят'}), 400
        item = Counterparty(code=code, name=data['name'], inn=data.get('inn'), type=data.get('type', 'ЮрЛицо'))
        db.session.add(item)
        db.session.commit()
        return jsonify(CounterpartySchema().dump(item)), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

def _apply_cp_fields(item, data, exclude_id=None):
    if 'code' in data:
        code = (data.get('code') or '').strip()
        err = _check_code_len(code)
        if err:
            return err
        if code and _code_taken(Counterparty, code, exclude_id):
            return f'Код {code} уже занят'
        item.code = code
    if 'name' in data: item.name = data['name']
    if 'inn' in data: item.inn = data['inn']
    if 'type' in data: item.type = data['type']
    return None

@app.route('/api/counterparties/<int:id>', methods=['PUT', 'PATCH'])
def update_counterparty(id):
    item = Counterparty.query.get_or_404(id)
    data = request.get_json(force=True, silent=True) or {}
    try:
        err = _apply_cp_fields(item, data, exclude_id=id)
        if err:
            return jsonify({'error': err}), 400
        db.session.commit()
        return jsonify(CounterpartySchema().dump(item))
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/counterparties/<int:id>', methods=['DELETE'])
def delete_counterparty(id):
    item = Counterparty.query.get_or_404(id)
    try:
        db.session.delete(item)
        db.session.commit()
        return jsonify({'message': 'Контрагент удалён', 'id': id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/counterparties/by-code/<code>', methods=['GET'])
def get_counterparty_by_code(code):
    item = Counterparty.query.filter_by(code=code).first_or_404()
    return jsonify(CounterpartySchema().dump(item))

@app.route('/api/counterparties/by-code/<code>', methods=['PUT', 'PATCH'])
def update_counterparty_by_code(code):
    item = Counterparty.query.filter_by(code=code).first_or_404()
    data = request.get_json(force=True, silent=True) or {}
    try:
        err = _apply_cp_fields(item, data, exclude_id=item.id)
        if err:
            return jsonify({'error': err}), 400
        db.session.commit()
        return jsonify(CounterpartySchema().dump(item))
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/counterparties/by-code/<code>', methods=['DELETE'])
def delete_counterparty_by_code(code):
    item = Counterparty.query.filter_by(code=code).first_or_404()
    try:
        db.session.delete(item)
        db.session.commit()
        return jsonify({'message': 'Контрагент удалён', 'code': code})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400


# ============================================================
# ЕДИНИЦЫ ИЗМЕРЕНИЯ
# ============================================================
@app.route('/api/units', methods=['GET'])
def get_units():
    return jsonify(UnitSchema(many=True).dump(Unit.query.all()))

@app.route('/api/units/<int:id>', methods=['GET'])
def get_unit(id):
    return jsonify(UnitSchema().dump(Unit.query.get_or_404(id)))

@app.route('/api/units', methods=['POST'])
def add_unit():
    data = request.get_json(force=True, silent=True) or {}
    try:
        if not data.get('name'):
            return jsonify({'error': 'Поле name обязательно'}), 400
        code = (data.get('code') or '').strip()
        if not code:
            code = generate_next_code(Unit)
        err = _check_code_len(code)
        if err:
            return jsonify({'error': err}), 400
        if code and _code_taken(Unit, code):
            return jsonify({'error': f'Код {code} уже занят'}), 400
        item = Unit(name=data['name'], code=code)
        db.session.add(item)
        db.session.commit()
        return jsonify(UnitSchema().dump(item)), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

def _apply_unit_fields(item, data, exclude_id=None):
    if 'code' in data:
        code = (data.get('code') or '').strip()
        err = _check_code_len(code)
        if err:
            return err
        if code and _code_taken(Unit, code, exclude_id):
            return f'Код {code} уже занят'
        item.code = code
    if 'name' in data: item.name = data['name']
    return None

@app.route('/api/units/<int:id>', methods=['PUT', 'PATCH'])
def update_unit(id):
    item = Unit.query.get_or_404(id)
    data = request.get_json(force=True, silent=True) or {}
    try:
        err = _apply_unit_fields(item, data, exclude_id=id)
        if err:
            return jsonify({'error': err}), 400
        db.session.commit()
        return jsonify(UnitSchema().dump(item))
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/units/<int:id>', methods=['DELETE'])
def delete_unit(id):
    item = Unit.query.get_or_404(id)
    try:
        db.session.delete(item)
        db.session.commit()
        return jsonify({'message': 'Единица измерения удалена', 'id': id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/units/by-code/<code>', methods=['GET'])
def get_unit_by_code(code):
    item = Unit.query.filter_by(code=code).first_or_404()
    return jsonify(UnitSchema().dump(item))

@app.route('/api/units/by-code/<code>', methods=['PUT', 'PATCH'])
def update_unit_by_code(code):
    item = Unit.query.filter_by(code=code).first_or_404()
    data = request.get_json(force=True, silent=True) or {}
    try:
        err = _apply_unit_fields(item, data, exclude_id=item.id)
        if err:
            return jsonify({'error': err}), 400
        db.session.commit()
        return jsonify(UnitSchema().dump(item))
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/units/by-code/<code>', methods=['DELETE'])
def delete_unit_by_code(code):
    item = Unit.query.filter_by(code=code).first_or_404()
    try:
        db.session.delete(item)
        db.session.commit()
        return jsonify({'message': 'Единица измерения удалена', 'code': code})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400


# ============================================================
# НОМЕНКЛАТУРА
# ============================================================
@app.route('/api/nomenclatures', methods=['GET'])
def get_nomenclatures():
    return jsonify(NomenclatureSchema(many=True).dump(Nomenclature.query.all()))

@app.route('/api/nomenclatures/<int:id>', methods=['GET'])
def get_nomenclature(id):
    return jsonify(NomenclatureSchema().dump(Nomenclature.query.get_or_404(id)))

@app.route('/api/nomenclatures', methods=['POST'])
def add_nomenclature():
    data = request.get_json(force=True, silent=True) or {}
    try:
        if not data.get('name'):
            return jsonify({'error': 'Поле name обязательно'}), 400
        code = (data.get('code') or '').strip()
        if not code:
            code = generate_next_code(Nomenclature)
        err = _check_code_len(code)
        if err:
            return jsonify({'error': err}), 400
        if code and _code_taken(Nomenclature, code):
            return jsonify({'error': f'Код {code} уже занят'}), 400
        item = Nomenclature(code=code, name=data['name'], unit_id=data['unit_id'], price=data.get('price', 0))
        db.session.add(item)
        db.session.commit()
        return jsonify(NomenclatureSchema().dump(item)), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

def _apply_nom_fields(item, data, exclude_id=None):
    if 'code' in data:
        code = (data.get('code') or '').strip()
        err = _check_code_len(code)
        if err:
            return err
        if code and _code_taken(Nomenclature, code, exclude_id):
            return f'Код {code} уже занят'
        item.code = code
    if 'name' in data: item.name = data['name']
    if 'unit_id' in data: item.unit_id = data['unit_id']
    if 'price' in data: item.price = data['price']
    return None

@app.route('/api/nomenclatures/<int:id>', methods=['PUT', 'PATCH'])
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

@app.route('/api/nomenclatures/<int:id>', methods=['DELETE'])
def delete_nomenclature(id):
    item = Nomenclature.query.get_or_404(id)
    try:
        db.session.delete(item)
        db.session.commit()
        return jsonify({'message': 'Номенклатура удалена', 'id': id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/nomenclatures/by-code/<code>', methods=['GET'])
def get_nomenclature_by_code(code):
    item = Nomenclature.query.filter_by(code=code).first_or_404()
    return jsonify(NomenclatureSchema().dump(item))

@app.route('/api/nomenclatures/by-code/<code>', methods=['PUT', 'PATCH'])
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

@app.route('/api/nomenclatures/by-code/<code>', methods=['DELETE'])
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
# ДОКУМЕНТ: РЕАЛИЗАЦИЯ
# ============================================================
@app.route('/api/sales', methods=['GET'])
def get_sales():
    return jsonify(SalesDocumentSchema(many=True).dump(SalesDocument.query.all()))

@app.route('/api/sales/<int:id>', methods=['GET'])
def get_sale(id):
    return jsonify(SalesDocumentSchema().dump(SalesDocument.query.get_or_404(id)))

def _number_taken(number, exclude_id=None):
    number = (number or '').strip()
    if not number:
        return False
    q = SalesDocument.query.filter(SalesDocument.number == number)
    if exclude_id is not None:
        q = q.filter(SalesDocument.id != exclude_id)
    return q.first() is not None

def _apply_sale_fields(doc, data, exclude_id=None):
    if 'number' in data:
        number = (data.get('number') or '').strip()
        if not number:
            return 'Поле number обязательно'
        err = _check_number_len(number)
        if err:
            return err
        if _number_taken(number, exclude_id):
            return f'Номер {number} уже занят'
        doc.number = number
    if 'date' in data: doc.doc_date = data['date']
    if 'doc_date' in data: doc.doc_date = data['doc_date']
    if 'organization_id' in data: doc.organization_id = data['organization_id']
    if 'counterparty_id' in data: doc.counterparty_id = data['counterparty_id']
    if 'comment' in data: doc.comment = data['comment']
    return None

@app.route('/api/sales', methods=['POST'])
def add_sales_document():
    data = request.get_json(force=True, silent=True) or {}
    try:
        if not data.get('number'):
            return jsonify({'error': 'Поле number обязательно'}), 400
        err = _check_number_len((data.get('number') or '').strip())
        if err:
            return jsonify({'error': err}), 400
        if _number_taken((data.get('number') or '').strip()):
            return jsonify({'error': f"Номер {data.get('number')} уже занят"}), 400
        doc = SalesDocument(
            number=(data['number'] or '').strip(),
            doc_date=data.get('date') or data.get('doc_date') or date.today(),
            organization_id=data['organization_id'],
            counterparty_id=data['counterparty_id'],
            comment=data.get('comment', '')
        )
        db.session.add(doc)
        db.session.flush()

        total = 0
        for item_data in (data.get('items') or []):
            item = SalesDocumentItem(
                document_id=doc.id,
                nomenclature_id=item_data['nomenclature_id'],
                quantity=item_data['quantity'],
                price=item_data['price']
            )
            item.sum = item.quantity * item.price
            total += item.sum
            db.session.add(item)

        doc.total_sum = total
        db.session.commit()
        return jsonify(SalesDocumentSchema().dump(doc)), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

def _replace_sale_items(doc, items_data):
    SalesDocumentItem.query.filter_by(document_id=doc.id).delete()
    total = 0
    for item_data in items_data:
        item = SalesDocumentItem(
            document_id=doc.id,
            nomenclature_id=item_data['nomenclature_id'],
            quantity=item_data['quantity'],
            price=item_data['price']
        )
        item.sum = item.quantity * item.price
        total += item.sum
        db.session.add(item)
    doc.total_sum = total

@app.route('/api/sales/<int:id>', methods=['PUT', 'PATCH'])
def update_sale(id):
    doc = SalesDocument.query.get_or_404(id)
    data = request.get_json(force=True, silent=True) or {}
    try:
        err = _apply_sale_fields(doc, data, exclude_id=id)
        if err:
            return jsonify({'error': err}), 400
        if 'items' in data:
            _replace_sale_items(doc, data['items'] or [])
        db.session.commit()
        return jsonify(SalesDocumentSchema().dump(doc))
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/sales/<int:id>', methods=['DELETE'])
def delete_sale(id):
    doc = SalesDocument.query.get_or_404(id)
    try:
        db.session.delete(doc)
        db.session.commit()
        return jsonify({'message': 'Документ удалён', 'id': id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

# --- Работа по номеру документа (для синхронизации с 1С) ---
@app.route('/api/sales/by-number/<number>', methods=['GET'])
def get_sale_by_number(number):
    doc = SalesDocument.query.filter_by(number=number).first_or_404()
    return jsonify(SalesDocumentSchema().dump(doc))

@app.route('/api/sales/by-number/<number>', methods=['PUT', 'PATCH'])
def update_sale_by_number(number):
    doc = SalesDocument.query.filter_by(number=number).first_or_404()
    data = request.get_json(force=True, silent=True) or {}
    try:
        err = _apply_sale_fields(doc, data, exclude_id=doc.id)
        if err:
            return jsonify({'error': err}), 400
        if 'items' in data:
            _replace_sale_items(doc, data['items'] or [])
        db.session.commit()
        return jsonify(SalesDocumentSchema().dump(doc))
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/sales/by-number/<number>', methods=['DELETE'])
def delete_sale_by_number(number):
    doc = SalesDocument.query.filter_by(number=number).first_or_404()
    try:
        db.session.delete(doc)
        db.session.commit()
        return jsonify({'message': 'Документ удалён', 'number': number})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400


# ============================================================
# СЛЕДУЮЩИЙ СВОБОДНЫЙ КОД (для подстановки в формы добавления)
# ============================================================
@app.route('/api/next-code/<entity>', methods=['GET'])
def get_next_code(entity):
    mapping = {
        'organizations': Organization,
        'counterparties': Counterparty,
        'units': Unit,
        'nomenclatures': Nomenclature,
    }
    model = mapping.get(entity)
    if model is None:
        return jsonify({'error': 'Неизвестный тип: ' + str(entity)}), 400
    return jsonify({'code': generate_next_code(model)})


# ============================================================
# ИНТЕГРАЦИЯ С 1С: НАСТРОЙКИ, ОТПРАВКА, ЖУРНАЛ
# ============================================================
@app.route('/api/settings_1c', methods=['GET'])
def get_settings_1c():
    return jsonify(public_1c_settings())

@app.route('/api/settings_1c', methods=['PUT'])
def update_settings_1c():
    data = request.get_json(force=True, silent=True) or {}
    # если пароль прислали как *** — значит не меняли, убираем из сохранения
    if data.get('password') in (None, '', '***'):
        data.pop('password', None)
    try:
        if 'timeout' in data:
            data['timeout'] = int(data['timeout'])
    except Exception:
        return jsonify({'error': 'timeout должен быть числом'}), 400
    saved = save_1c_settings(data)
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
    """Проверка связи с 1С: простой GET на base_url."""
    settings = load_1c_settings()
    data = request.get_json(force=True, silent=True) or {}
    base_url = (data.get('base_url') or settings.get('base_url', '')).strip()
    if not base_url:
        return jsonify({'success': False, 'error': 'Не задан base_url'}), 400
    # временно подменяем base_url для теста
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
    """Сформировать JSON для отправки в 1С из записи БД (для предпросмотра в UI)."""
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
    """
    Универсальная отправка в 1С.
    Принимает: {
      "entity": "sales" | "organizations" | ... | "custom",
      "record_id": 1,          // необязательно, если body передан вручную
      "method": "POST",        // GET/POST/PUT/PATCH/DELETE
      "path": "/sales",        // путь отправки, можно менять в UI
      "body": {...},           // необязательно, иначе соберём из БД
      "base_url": "..."        // необязательно, иначе из настроек
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    settings = load_1c_settings()

    entity = data.get('entity', 'custom')
    record_id = data.get('record_id')
    method = (data.get('method') or settings.get('default_method') or 'POST').upper()
    path = data.get('path')
    if not path:
        # подставить путь из настроек по типу сущности
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
    # для GET/DELETE тело обычно не нужно
    if method in ('GET', 'DELETE') and body is None:
        body = None

    req_text = '' if body is None else (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False))
    status, resp_text, err = do_http_to_1c(url, method, body, settings)

    success = err is None and status is not None and 200 <= status < 300
    # пишем в журнал
    try:
        log = SendLog(
            entity=entity,
            record_id=record_id,
            method=method,
            url=url,
            request_body=req_text[:8000] if isinstance(req_text, str) else '',
            response_status=status,
            response_body=(resp_text or '')[:8000],
            success=success,
            error=(err or '')
        )
        db.session.add(log)
        db.session.commit()
        log_id = log.id
    except Exception as e:
        db.session.rollback()
        log_id = None

    code = 200 if success else 502
    return jsonify({
        'success': success,
        'status': status,
        'url': url,
        'method': method,
        'response': resp_text,
        'error': err,
        'log_id': log_id
    }), code


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
        .warning strong { color: #856404; }
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
        .modal-content .doc-detail-header strong { color: #333; }
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
        .sale-items-edit td input, .sale-items-edit td select { width: 100%; padding: 4px 6px; border: 1px solid #ced4da; border-radius: 4px; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🚀 Веб-сервис</h1>
        <div class="subtitle">Учебный стенд для 1С-разработчиков</div>

        <div class="info-box">
            <span>📍 Адрес: <strong>192.168.85.77:8080</strong></span>
            <span>📦 База: <strong>PostgreSQL</strong></span>
            <span>📌 API: <strong>/api/...</strong></span>
        </div>

        <div class="warning">
            ℹ️ Добавлять элементы справочников и документы можно прямо здесь (формы <strong>➕ Добавить</strong>), а также через <strong>1С</strong> или <strong>Postman</strong>. Код подставляется автоматически в формате <strong>ВБ-000000001</strong>.
        </div>

        <!-- ===== НАВИГАЦИЯ ===== -->
        <div class="section-label">📚 Справочники</div>
        <div class="nav-list">
            <div class="nav-item active" data-page="page-orgs" onclick="navigateTo('page-orgs')">
                <div class="name">🏢 Организации</div>
            </div>
            <div class="nav-item" data-page="page-counterparties" onclick="navigateTo('page-counterparties')">
                <div class="name">🤝 Контрагенты</div>
            </div>
            <div class="nav-item" data-page="page-units" onclick="navigateTo('page-units')">
                <div class="name">📏 Единицы измерения</div>
            </div>
            <div class="nav-item" data-page="page-nomenclatures" onclick="navigateTo('page-nomenclatures')">
                <div class="name">📦 Номенклатура</div>
            </div>
        </div>

        <div class="section-label">📄 Документы</div>
        <div class="nav-list">
            <div class="nav-item" data-page="page-sales" onclick="navigateTo('page-sales')">
                <div class="name">📤 Реализация</div>
            </div>
        </div>

        <div class="section-label">🔗 Интеграция с 1С</div>
        <div class="nav-list">
            <div class="nav-item send1c" data-page="page-1c" onclick="navigateTo('page-1c')">
                <div class="name">🚀 Отправка в 1С</div>
            </div>
        </div>

        <!-- ===== СТРАНИЦЫ ===== -->
        <div id="page-orgs" class="page active">
            <div class="card">
                <div class="card-header">
                    <h2>🏢 Организации</h2>
                    <span class="badge">GET /api/organizations · by-code /api/organizations/by-code/{code}</span>
                </div>
                <div class="hint" style="margin-bottom:8px;">Ключ синхронизации — <b>code</b> как в 1С (12 символов, формат ВБ-000000001). PATCH/PUT/DELETE работают и по <b>id</b>, и по <b>code</b>.</div>
                <div id="orgs-list"><div class="loading">Загрузка...</div></div>
                <div class="row-flex" style="margin-top:8px;">
                    <button class="btn btn-sm green" onclick="showTemplate('org')">📋 Шаблоны (POST + by-code)</button>
                    <button class="btn btn-sm" onclick="toggleAddForm('add-org')">➕ Добавить</button>
                </div>
                <div id="template-org" class="template-block"></div>
                <div id="add-org" class="add-form" style="display:none;">
                    <h4>➕ Новая организация</h4>
                    <div class="form-grid">
                        <label>Код</label>
                        <div class="code-row"><input id="new-org-code" maxlength="12" placeholder="ВБ-000000001" /><button class="btn btn-sm" title="Следующий свободный код" onclick="prefillNextCode('organizations','new-org-code')">↻</button></div>
                        <label>Наименование *</label><input id="new-org-name" placeholder="ООО Ромашка" />
                        <label>ИНН</label><input id="new-org-inn" maxlength="12" placeholder="1234567890" />
                        <label>КПП</label><input id="new-org-kpp" maxlength="9" placeholder="123456789" />
                    </div>
                    <div class="row-flex"><button class="btn green" onclick="createOrg()">💾 Сохранить</button><span id="new-org-status"></span></div>
                    <div class="hint">Если код не менять — подставится следующий свободный вида ВБ-000000001.</div>
                </div>
            </div>
        </div>

        <div id="page-counterparties" class="page">
            <div class="card">
                <div class="card-header">
                    <h2>🤝 Контрагенты</h2>
                    <span class="badge">GET /api/counterparties · by-code /api/counterparties/by-code/{code}</span>
                </div>
                <div class="hint" style="margin-bottom:8px;">Ключ синхронизации — <b>code</b> как в 1С (12 символов, формат ВБ-000000001). PATCH/PUT/DELETE работают и по <b>id</b>, и по <b>code</b>.</div>
                <div id="counterparties-list"><div class="loading">Загрузка...</div></div>
                <div class="row-flex" style="margin-top:8px;">
                    <button class="btn btn-sm green" onclick="showTemplate('counterparty')">📋 Шаблоны (POST + by-code)</button>
                    <button class="btn btn-sm" onclick="toggleAddForm('add-cp')">➕ Добавить</button>
                </div>
                <div id="template-counterparty" class="template-block"></div>
                <div id="add-cp" class="add-form" style="display:none;">
                    <h4>➕ Новый контрагент</h4>
                    <div class="form-grid">
                        <label>Код</label>
                        <div class="code-row"><input id="new-cp-code" maxlength="12" placeholder="ВБ-000000001" /><button class="btn btn-sm" title="Следующий свободный код" onclick="prefillNextCode('counterparties','new-cp-code')">↻</button></div>
                        <label>Наименование *</label><input id="new-cp-name" placeholder="ИП Петров" />
                        <label>ИНН</label><input id="new-cp-inn" maxlength="12" placeholder="9876543210" />
                        <label>Тип</label><select id="new-cp-type"><option>ЮрЛицо</option><option>ФизЛицо</option><option>ИП</option></select>
                    </div>
                    <div class="row-flex"><button class="btn green" onclick="createCp()">💾 Сохранить</button><span id="new-cp-status"></span></div>
                    <div class="hint">Если код не менять — подставится следующий свободный вида ВБ-000000001.</div>
                </div>
            </div>
        </div>

        <div id="page-units" class="page">
            <div class="card">
                <div class="card-header">
                    <h2>📏 Единицы измерения</h2>
                    <span class="badge">GET /api/units · by-code /api/units/by-code/{code}</span>
                </div>
                <div class="hint" style="margin-bottom:8px;">Ключ синхронизации — <b>code</b> как в 1С (12 символов, формат ВБ-000000001). PATCH/PUT/DELETE работают и по <b>id</b>, и по <b>code</b>.</div>
                <div id="units-list"><div class="loading">Загрузка...</div></div>
                <div class="row-flex" style="margin-top:8px;">
                    <button class="btn btn-sm green" onclick="showTemplate('unit')">📋 Шаблоны (POST + by-code)</button>
                    <button class="btn btn-sm" onclick="toggleAddForm('add-unit')">➕ Добавить</button>
                </div>
                <div id="template-unit" class="template-block"></div>
                <div id="add-unit" class="add-form" style="display:none;">
                    <h4>➕ Новая единица измерения</h4>
                    <div class="form-grid">
                        <label>Код</label>
                        <div class="code-row"><input id="new-unit-code" maxlength="12" placeholder="ВБ-000000001" /><button class="btn btn-sm" title="Следующий свободный код" onclick="prefillNextCode('units','new-unit-code')">↻</button></div>
                        <label>Наименование *</label><input id="new-unit-name" placeholder="Штука" />
                    </div>
                    <div class="row-flex"><button class="btn green" onclick="createUnit()">💾 Сохранить</button><span id="new-unit-status"></span></div>
                    <div class="hint">Если код не менять — подставится следующий свободный вида ВБ-000000001.</div>
                </div>
            </div>
        </div>

        <div id="page-nomenclatures" class="page">
            <div class="card">
                <div class="card-header">
                    <h2>📦 Номенклатура</h2>
                    <span class="badge">GET /api/nomenclatures · by-code /api/nomenclatures/by-code/{code}</span>
                </div>
                <div class="hint" style="margin-bottom:8px;">Ключ синхронизации — <b>code</b> как в 1С (12 символов, формат ВБ-000000001). PATCH/PUT/DELETE работают и по <b>id</b>, и по <b>code</b>.</div>
                <div id="nomenclatures-list"><div class="loading">Загрузка...</div></div>
                <div class="row-flex" style="margin-top:8px;">
                    <button class="btn btn-sm green" onclick="showTemplate('nomenclature')">📋 Шаблоны (POST + by-code)</button>
                    <button class="btn btn-sm" onclick="toggleAddForm('add-nom');loadUnitsIntoNomForm()">➕ Добавить</button>
                </div>
                <div id="template-nomenclature" class="template-block"></div>
                <div id="add-nom" class="add-form" style="display:none;">
                    <h4>➕ Новая номенклатура</h4>
                    <div class="form-grid">
                        <label>Код</label>
                        <div class="code-row"><input id="new-nom-code" maxlength="12" placeholder="ВБ-000000001" /><button class="btn btn-sm" title="Следующий свободный код" onclick="prefillNextCode('nomenclatures','new-nom-code')">↻</button></div>
                        <label>Наименование *</label><input id="new-nom-name" placeholder="Товар 1" />
                        <label>Единица</label><select id="new-nom-unit"><option value="">—</option></select>
                        <label>Цена</label><input id="new-nom-price" type="number" step="0.01" min="0" value="0" />
                    </div>
                    <div class="row-flex"><button class="btn green" onclick="createNom()">💾 Сохранить</button><span id="new-nom-status"></span></div>
                    <div class="hint">Если код не менять — подставится следующий свободный вида ВБ-000000001.</div>
                </div>
            </div>
        </div>

        <div id="page-sales" class="page">
            <div class="card">
                <div class="card-header">
                    <h2>📤 Реализация</h2>
                    <span class="badge">GET /api/sales · by-number /api/sales/by-number/{number}</span>
                </div>
                <div class="hint" style="margin-bottom:8px;">Ключ синхронизации — <b>number</b> документа (11 символов). PATCH/PUT/DELETE работают и по <b>id</b>, и по <b>number</b>.</div>
                <div class="row-flex">
                    <button class="btn btn-sm green" onclick="showTemplate('sales')">📋 Шаблоны (POST + by-number)</button>
                    <button class="btn btn-sm" onclick="toggleAddForm('add-sale');loadSaleFormRefs()">➕ Добавить документ</button>
                </div>
                <div id="template-sales" class="template-block"></div>
                <div id="add-sale" class="add-form" style="display:none;">
                    <h4>➕ Новый документ реализации</h4>
                    <div class="form-grid">
                        <label>Номер *</label><input id="new-sale-number" maxlength="11" placeholder="РТУ-0000001" />
                        <label>Дата</label><input id="new-sale-date" type="date" />
                        <label>Организация *</label><select id="new-sale-org"><option value="">—</option></select>
                        <label>Контрагент *</label><select id="new-sale-cp"><option value="">—</option></select>
                        <label>Комментарий</label><input id="new-sale-comment" placeholder="Продажа" />
                    </div>
                    <h4 style="margin-top:10px;">📋 Табличная часть</h4>
                    <table class="sale-items-edit"><thead><tr><th>Номенклатура</th><th style="width:110px;">Кол-во</th><th style="width:130px;">Цена</th><th style="width:50px;"></th></tr></thead>
                    <tbody id="new-sale-items"></tbody></table>
                    <div class="row-flex" style="margin-top:8px;">
                        <button class="btn btn-sm" onclick="addSaleRow()">➕ Строка</button>
                        <span class="hint" id="new-sale-total">Итого: 0</span>
                    </div>
                    <div class="row-flex" style="margin-top:8px;"><button class="btn green" onclick="createSale()">💾 Провести документ</button><span id="new-sale-status"></span></div>
                </div>
                <div id="sales-list" style="margin-top:10px;"><div class="loading">Загрузка...</div></div>
            </div>
        </div>

        <div id="page-1c" class="page">
            <div class="card">
                <div class="card-header">
                    <h2>⚙️ Подключение к 1С</h2>
                    <span class="badge">GET/PUT /api/settings_1c</span>
                </div>
                <div class="hint">Base URL — это адрес публикации 1С до /hs/api. Например: <b>http://192.168.1.10/MyBase/hs/api</b>. Пути ниже дописываются к нему. Авторизация выключена по умолчанию.</div>
                <div class="form-grid">
                    <label>Base URL 1С</label>
                    <input id="s1c-base-url" placeholder="http://server/base/hs/api" />
                    <label>Таймаут (сек)</label>
                    <input id="s1c-timeout" type="number" min="1" max="120" />
                    <label>Проверять SSL</label>
                    <select id="s1c-verify"><option value="0">Нет (для http / самоподписанных)</option><option value="1">Да</option></select>
                    <label>Авторизация</label>
                    <select id="s1c-use-auth" onchange="toggleAuth1c()"><option value="0">Без авторизации</option><option value="1">Логин + пароль (Basic)</option></select>
                    <label class="auth1c-row">Логин 1С</label>
                    <input class="auth1c-row" id="s1c-login" placeholder="Администратор" />
                    <label class="auth1c-row">Пароль 1С</label>
                    <input class="auth1c-row" id="s1c-password" type="password" placeholder="оставьте *** чтобы не менять" />
                    <label>Метод по умолчанию</label>
                    <select id="s1c-method"><option>POST</option><option>PUT</option><option>PATCH</option><option>GET</option><option>DELETE</option></select>
                </div>
                <div class="section-label">Пути отправки (можно менять под свой HTTP-сервис 1С)</div>
                <div class="form-grid">
                    <label>Организации</label><input id="s1c-ep-organizations" placeholder="/organizations" />
                    <label>Контрагенты</label><input id="s1c-ep-counterparties" placeholder="/counterparties" />
                    <label>Единицы</label><input id="s1c-ep-units" placeholder="/units" />
                    <label>Номенклатура</label><input id="s1c-ep-nomenclatures" placeholder="/nomenclatures" />
                    <label>Реализация</label><input id="s1c-ep-sales" placeholder="/sales" />
                </div>
                <div class="row-flex">
                    <button class="btn green" onclick="saveSettings1c()">💾 Сохранить</button>
                    <button class="btn" onclick="testConnection1c()">🔌 Проверить связь</button>
                    <span id="s1c-settings-status"></span>
                </div>
            </div>

            <div class="card">
                <div class="card-header">
                    <h2>📨 Отправка данных в 1С</h2>
                    <span class="badge">POST /api/send_to_1c</span>
                </div>
                <div class="form-grid">
                    <label>Что отправляем</label>
                    <select id="send-entity" onchange="onEntityChange()">
                        <option value="organizations">🏢 Организация (из БД)</option>
                        <option value="counterparties">🤝 Контрагент (из БД)</option>
                        <option value="units">📏 Единица измерения (из БД)</option>
                        <option value="nomenclatures">📦 Номенклатура (из БД)</option>
                        <option value="sales" selected>📤 Реализация (из БД)</option>
                        <option value="custom">✏️ Произвольный JSON</option>
                    </select>
                    <label>Запись из БД</label>
                    <div class="row-flex"><select id="send-record" style="flex:1;min-width:200px;"></select><button class="btn btn-sm" onclick="loadRecordsForSend()">↻</button></div>
                    <label>Метод</label>
                    <select id="send-method"><option>POST</option><option>PUT</option><option>PATCH</option><option>GET</option><option>DELETE</option></select>
                    <label>Путь отправки</label>
                    <input id="send-path" placeholder="/sales" />
                    <label>Тело запроса (JSON)</label>
                    <textarea id="send-body" rows="10" placeholder='{"number":"РТУ-0000001",...}'></textarea>
                </div>
                <div class="row-flex">
                    <button class="btn" onclick="buildBodyPreview()">📥 Подставить из БД</button>
                    <button class="btn green" onclick="sendTo1c()">🚀 Отправить в 1С</button>
                    <span id="send-status"></span>
                </div>
                <div class="hint" style="margin-top:8px;">Полный URL собирается как: <b>Base URL + Путь</b>. Тело можно править вручную перед отправкой. Всё сохраняется в журнале ниже.</div>
            </div>

            <div class="card">
                <div class="card-header">
                    <h2>📜 Журнал отправок в 1С</h2>
                    <div class="row-flex"><button class="btn btn-sm" onclick="loadLog()">↻ Обновить</button><button class="btn btn-sm red" onclick="clearLog()">🗑️ Очистить</button></div>
                </div>
                <div id="log-list"><div class="loading">Журнал пуст</div></div>
            </div>
        </div>
    </div>

    <!-- ===== МОДАЛЬНОЕ ОКНО ===== -->
    <div id="modal" class="modal">
        <div class="modal-content">
            <span class="close" onclick="closeModal()">&times;</span>
            <h3 id="modal-title">📄 Просмотр JSON</h3>
            <div id="modal-body"></div>
        </div>
    </div>

    <script>
        // ============================================================
        // 1. НАВИГАЦИЯ
        // ============================================================
        function navigateTo(pageId) {
            document.querySelectorAll('.page').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
            document.getElementById(pageId).classList.add('active');
            const navItem = document.querySelector(`.nav-item[data-page="${pageId}"]`);
            if (navItem) navItem.classList.add('active');
            
            if (pageId === 'page-orgs') loadOrgs();
            else if (pageId === 'page-counterparties') loadCounterparties();
            else if (pageId === 'page-units') loadUnits();
            else if (pageId === 'page-nomenclatures') loadNomenclatures();
            else if (pageId === 'page-sales') loadSales();
            else if (pageId === 'page-1c') { loadSettings1c(); loadRecordsForSend(); loadLog(); }
        }

        // ============================================================
        // 2. ЗАГРУЗКА ДАННЫХ
        // ============================================================
        async function fetchData(url) {
            try {
                const r = await fetch(url);
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return await r.json();
            } catch(e) { return { error: e.message }; }
        }

        async function deleteItem(url, callback) {
            if (!confirm('Удалить запись?')) return;
            try {
                const r = await fetch(url, { method: 'DELETE' });
                const d = await r.json();
                if (!r.ok) throw new Error(d.error || 'Ошибка');
                alert('✅ Удалено');
                if (callback) callback();
            } catch(e) { alert('❌ Ошибка: ' + e.message); }
        }

        function renderTable(data, cols, apiBase, showDetails=false, entity='') {
            if (!data || data.error) return `<div class="status error">❌ ${data?.error || 'Ошибка'}</div>`;
            if (!data.length) return `<div class="status empty">📭 Нет записей</div>`;
            let html = '<table><thead><tr>';
            cols.forEach(c => html += `<th>${c}</th>`);
            html += '<th style="width:60px;">JSON</th>';
            if (showDetails) html += '<th style="width:60px;">ТЧ</th>';
            html += '<th style="width:60px;">В 1С</th>';
            html += '<th style="width:60px;">Удалить</th>';
            html += '</tr></thead><tbody>';
            data.forEach(item => {
                html += '<tr>';
                cols.forEach(c => {
                    let v = item[c] ?? '';
                    if (typeof v === 'object') v = JSON.stringify(v);
                    if (typeof v === 'string' && v.length > 40) v = v.substring(0,40)+'...';
                    html += `<td>${v}</td>`;
                });
                html += `<td><button class="btn btn-sm" onclick="viewJson('${apiBase}/${item.id}')">📄</button></td>`;
                if (showDetails) {
                    html += `<td><button class="btn btn-sm orange" onclick="viewDoc('${apiBase}/${item.id}')">📋</button></td>`;
                }
                html += `<td><button class="btn btn-sm green" title="Отправить в 1С" onclick="quickSendTo1c('${entity}', ${item.id})">🚀</button></td>`;
                html += `<td><button class="btn btn-sm red" onclick="deleteItem('${apiBase}/${item.id}', () => navigateTo(document.querySelector('.page.active').id))">🗑️</button></td>`;
                html += '</tr>';
            });
            html += '</tbody></table>';
            return html;
        }

        // ---------- СПРАВОЧНИКИ ----------
        async function loadOrgs() {
            const d = await fetchData('/api/organizations');
            document.getElementById('orgs-list').innerHTML = renderTable(d, ['id','code','name','inn','kpp'], '/api/organizations', false, 'organizations');
        }
        async function loadCounterparties() {
            const d = await fetchData('/api/counterparties');
            document.getElementById('counterparties-list').innerHTML = renderTable(d, ['id','code','name','inn','type'], '/api/counterparties', false, 'counterparties');
        }
        async function loadUnits() {
            const d = await fetchData('/api/units');
            document.getElementById('units-list').innerHTML = renderTable(d, ['id','code','name'], '/api/units', false, 'units');
        }
        async function loadNomenclatures() {
            const d = await fetchData('/api/nomenclatures');
            document.getElementById('nomenclatures-list').innerHTML = renderTable(d, ['id','code','name','unit_name','price'], '/api/nomenclatures', false, 'nomenclatures');
        }

        // ---------- ДОКУМЕНТЫ ----------
        async function loadSales() {
            const d = await fetchData('/api/sales');
            if (!d || d.error) { document.getElementById('sales-list').innerHTML = `<div class="status error">❌ ${d?.error||'Ошибка'}</div>`; return; }
            if (!d.length) { document.getElementById('sales-list').innerHTML = `<div class="status empty">📭 Нет документов</div>`; return; }
            let html = '<table><thead><tr><th>ID</th><th>Номер</th><th>Дата</th><th>Орг-ция</th><th>Контрагент</th><th>Сумма</th><th style="width:60px;">JSON</th><th style="width:60px;">ТЧ</th><th style="width:60px;">В 1С</th><th style="width:60px;">Удалить</th></tr></thead><tbody>';
            d.forEach(doc => {
                html += `<tr><td>${doc.id}</td><td>${doc.number}</td><td>${doc.doc_date}</td><td>${doc.organization_name||''}</td><td>${doc.counterparty_name||''}</td><td>${doc.total_sum}</td><td><button class="btn btn-sm" onclick="viewJson('/api/sales/${doc.id}')">📄</button></td><td><button class="btn btn-sm orange" onclick="viewDoc('/api/sales/${doc.id}')">📋</button></td><td><button class="btn btn-sm green" title="Отправить в 1С" onclick="quickSendTo1c('sales', ${doc.id})">🚀</button></td><td><button class="btn btn-sm red" onclick="deleteItem('/api/sales/${doc.id}', loadSales)">🗑️</button></td></tr>`;
            });
            html += '</tbody></table>';
            document.getElementById('sales-list').innerHTML = html;
        }

        // ============================================================
        // 3. ПРОСМОТР JSON
        // ============================================================
        async function viewJson(url) {
            const d = await fetchData(url);
            if (d.error) { alert('Ошибка: '+d.error); return; }
            document.getElementById('modal-title').textContent = '📄 '+url;
            document.getElementById('modal-body').innerHTML = `
                <div class="json-view">${JSON.stringify(d, null, 2)}</div>
                <button class="btn btn-sm green" onclick="copyJson()" style="margin-top:10px;">📋 Копировать</button>
                <span id="copy-status" style="display:none;color:#28a745;margin-left:10px;">✅ Скопировано!</span>
            `;
            document.getElementById('modal').style.display = 'flex';
            window._json = d;
        }

        function copyJson() {
            if (window._json) {
                const text = JSON.stringify(window._json, null, 2);
                const ta = document.createElement('textarea');
                ta.value = text;
                document.body.appendChild(ta);
                ta.select();
                try { document.execCommand('copy'); } catch(e) {}
                document.body.removeChild(ta);
                const s = document.getElementById('copy-status');
                if (s) { s.style.display = 'inline'; setTimeout(() => s.style.display = 'none', 2000); }
            }
        }

        // ============================================================
        // 4. ПРОСМОТР ДОКУМЕНТА С ТЧ
        // ============================================================
        async function viewDoc(url) {
            const d = await fetchData(url);
            if (d.error) { alert('Ошибка: '+d.error); return; }
            let itemsHtml = '<div class="status empty">📭 Нет строк в ТЧ</div>';
            if (d.items && d.items.length) {
                itemsHtml = '<table class="items-table"><thead><tr><th>ID</th><th>Номенклатура</th><th>Кол-во</th><th>Цена</th><th>Сумма</th></tr></thead><tbody>';
                d.items.forEach(it => {
                    itemsHtml += `<tr><td>${it.id}</td><td>${it.nomenclature_name||it.nomenclature_id}</td><td>${it.quantity}</td><td>${it.price}</td><td>${it.sum}</td></tr>`;
                });
                itemsHtml += '</tbody></table>';
            }
            document.getElementById('modal-title').textContent = `📄 Реализация №${d.number} (ID: ${d.id})`;
            document.getElementById('modal-body').innerHTML = `
                <div class="doc-detail-header">
                    <div><strong>Номер:</strong> ${d.number}</div>
                    <div><strong>Дата:</strong> ${d.doc_date}</div>
                    <div><strong>Организация:</strong> ${d.organization_name||d.organization_id}</div>
                    <div><strong>Контрагент:</strong> ${d.counterparty_name||d.counterparty_id}</div>
                    <div><strong>Сумма:</strong> ${d.total_sum}</div>
                    <div><strong>Комментарий:</strong> ${d.comment||''}</div>
                </div>
                <h4 style="margin:10px 0;">📋 Табличная часть</h4>
                ${itemsHtml}
                <div style="margin-top:15px;">
                    <button class="btn btn-sm green" onclick="viewJson('${url}')">📄 Полный JSON</button>
                </div>
            `;
            document.getElementById('modal').style.display = 'flex';
        }

        function closeModal() {
            document.getElementById('modal').style.display = 'none';
        }
        document.getElementById('modal').addEventListener('click', function(e) {
            if (e.target === this) closeModal();
        });

        // ============================================================
        // 5. ШАБЛОНЫ JSON
        // ============================================================
        const templates = {
            'org': { name: 'POST /api/organizations | PUT/PATCH/DELETE /api/organizations/by-code/{code}', json: JSON.stringify({code:"ВБ-000000001",name:"ООО Ромашка",inn:"1234567890",kpp:"123456789"}, null, 2) },
            'counterparty': { name: 'POST /api/counterparties | PUT/PATCH/DELETE /api/counterparties/by-code/{code}', json: JSON.stringify({code:"ВБ-000000001",name:"ИП Петров",inn:"9876543210",type:"ЮрЛицо"}, null, 2) },
            'unit': { name: 'POST /api/units | PUT/PATCH/DELETE /api/units/by-code/{code}', json: JSON.stringify({code:"ВБ-000000001",name:"Штука"}, null, 2) },
            'nomenclature': { name: 'POST /api/nomenclatures | PUT/PATCH/DELETE /api/nomenclatures/by-code/{code}', json: JSON.stringify({code:"ВБ-000000001",name:"Товар 1",unit_id:1,price:100.50}, null, 2) },
            'sales': { name: 'POST /api/sales | PUT/PATCH/DELETE /api/sales/by-number/{number}', json: JSON.stringify({number:"РТУ-0000001",organization_id:1,counterparty_id:1,comment:"Продажа",items:[{nomenclature_id:1,quantity:5,price:100}]}, null, 2) }
        };

        function showTemplate(type) {
            const div = document.getElementById('template-'+type);
            if (div.style.display === 'block') { div.style.display = 'none'; return; }
            const t = templates[type];
            if (!t) return;
            div.innerHTML = `
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px;">
                    <strong>${t.name}</strong>
                    <button class="btn btn-sm" onclick="copyTemplate('${type}')">📋 Копировать</button>
                    <span id="copy-tpl-${type}" style="display:none;color:#28a745;font-size:12px;">✅ Скопировано!</span>
                </div>
                <pre>${t.json}</pre>
            `;
            div.style.display = 'block';
        }

        function copyTemplate(type) {
            const t = templates[type];
            if (!t) return;
            const text = t.json;
            const ta = document.createElement('textarea');
            ta.value = text;
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand('copy'); } catch(e) {}
            document.body.removeChild(ta);
            const s = document.getElementById('copy-tpl-'+type);
            if (s) { s.style.display = 'inline'; setTimeout(() => s.style.display = 'none', 2000); }
        }

        // ============================================================
        // 5.1 ДОБАВЛЕНИЕ ЭЛЕМЕНТОВ И ДОКУМЕНТОВ
        // ============================================================
        function toggleAddForm(id) {
            const el = document.getElementById(id);
            if (!el) return;
            const show = el.style.display === 'none';
            el.style.display = show ? 'block' : 'none';
            if (show) {
                if (id === 'add-org') prefillNextCode('organizations', 'new-org-code');
                if (id === 'add-cp') prefillNextCode('counterparties', 'new-cp-code');
                if (id === 'add-unit') prefillNextCode('units', 'new-unit-code');
                if (id === 'add-nom') { prefillNextCode('nomenclatures', 'new-nom-code'); loadUnitsIntoNomForm(); }
                if (id === 'add-sale') {
                    loadSaleFormRefs();
                    if (!document.getElementById('new-sale-date').value)
                        document.getElementById('new-sale-date').valueAsDate = new Date();
                    if (!document.querySelectorAll('#new-sale-items tr').length) addSaleRow();
                }
            }
        }
        async function prefillNextCode(entity, inputId) {
            const inp = document.getElementById(inputId);
            if (!inp || inp.value) return;
            try {
                const r = await fetch('/api/next-code/' + entity);
                const d = await r.json();
                if (r.ok && d.code) inp.value = d.code;
            } catch(e) {}
        }
        async function postJson(url, payload) {
            const r = await fetch(url, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
            return d;
        }
        function setStatus(id, ok, text) {
            document.getElementById(id).innerHTML = ok ? `<span class="ok-text">✅ ${text}</span>` : `<span class="err-text">❌ ${text}</span>`;
        }
        async function createOrg() {
            try {
                const d = await postJson('/api/organizations', {
                    code: document.getElementById('new-org-code').value.trim(),
                    name: document.getElementById('new-org-name').value.trim(),
                    inn: document.getElementById('new-org-inn').value.trim(),
                    kpp: document.getElementById('new-org-kpp').value.trim()
                });
                setStatus('new-org-status', true, `Сохранено: ${d.code} — ${d.name}`);
                document.getElementById('new-org-name').value = '';
                document.getElementById('new-org-code').value = '';
                prefillNextCode('organizations', 'new-org-code');
                loadOrgs();
            } catch(e) { setStatus('new-org-status', false, e.message); }
        }
        async function createCp() {
            try {
                const d = await postJson('/api/counterparties', {
                    code: document.getElementById('new-cp-code').value.trim(),
                    name: document.getElementById('new-cp-name').value.trim(),
                    inn: document.getElementById('new-cp-inn').value.trim(),
                    type: document.getElementById('new-cp-type').value
                });
                setStatus('new-cp-status', true, `Сохранено: ${d.code} — ${d.name}`);
                document.getElementById('new-cp-name').value = '';
                document.getElementById('new-cp-code').value = '';
                prefillNextCode('counterparties', 'new-cp-code');
                loadCounterparties();
            } catch(e) { setStatus('new-cp-status', false, e.message); }
        }
        async function createUnit() {
            try {
                const d = await postJson('/api/units', {
                    code: document.getElementById('new-unit-code').value.trim(),
                    name: document.getElementById('new-unit-name').value.trim()
                });
                setStatus('new-unit-status', true, `Сохранено: ${d.code} — ${d.name}`);
                document.getElementById('new-unit-name').value = '';
                document.getElementById('new-unit-code').value = '';
                prefillNextCode('units', 'new-unit-code');
                loadUnits();
            } catch(e) { setStatus('new-unit-status', false, e.message); }
        }
        async function loadUnitsIntoNomForm() {
            const sel = document.getElementById('new-nom-unit');
            if (!sel || sel.options.length > 1) return;
            const d = await fetchData('/api/units');
            if (d && !d.error) sel.innerHTML = '<option value="">—</option>' + d.map(u => `<option value="${u.id}">${u.code ? '[' + u.code + '] ' : ''}${u.name}</option>`).join('');
        }
        async function createNom() {
            try {
                const unitVal = document.getElementById('new-nom-unit').value;
                const d = await postJson('/api/nomenclatures', {
                    code: document.getElementById('new-nom-code').value.trim(),
                    name: document.getElementById('new-nom-name').value.trim(),
                    unit_id: unitVal ? parseInt(unitVal) : null,
                    price: parseFloat(document.getElementById('new-nom-price').value) || 0
                });
                setStatus('new-nom-status', true, `Сохранено: ${d.code} — ${d.name}`);
                document.getElementById('new-nom-name').value = '';
                document.getElementById('new-nom-code').value = '';
                prefillNextCode('nomenclatures', 'new-nom-code');
                loadNomenclatures();
            } catch(e) { setStatus('new-nom-status', false, e.message); }
        }
        // ----- документ реализации -----
        let _saleNoms = [];
        async function loadSaleFormRefs() {
            const [orgs, cps, noms] = await Promise.all([fetchData('/api/organizations'), fetchData('/api/counterparties'), fetchData('/api/nomenclatures')]);
            if (orgs && !orgs.error) document.getElementById('new-sale-org').innerHTML = '<option value="">—</option>' + orgs.map(o => `<option value="${o.id}">${o.code ? '[' + o.code + '] ' : ''}${o.name}</option>`).join('');
            if (cps && !cps.error) document.getElementById('new-sale-cp').innerHTML = '<option value="">—</option>' + cps.map(c => `<option value="${c.id}">${c.code ? '[' + c.code + '] ' : ''}${c.name}</option>`).join('');
            if (noms && !noms.error) { _saleNoms = noms; refreshSaleNomSelects(); }
        }
        function refreshSaleNomSelects() {
            document.querySelectorAll('#new-sale-items select[data-nom]').forEach(sel => {
                const cur = sel.value;
                sel.innerHTML = _saleNoms.map(n => `<option value="${n.id}" data-price="${n.price}">${n.code ? '[' + n.code + '] ' : ''}${n.name} — ${n.price}</option>`).join('');
                if (cur) sel.value = cur;
            });
        }
        function addSaleRow() {
            const tb = document.getElementById('new-sale-items');
            const tr = document.createElement('tr');
            const opts = _saleNoms.map(n => `<option value="${n.id}" data-price="${n.price}">${n.code ? '[' + n.code + '] ' : ''}${n.name} — ${n.price}</option>`).join('');
            tr.innerHTML = `<td><select data-nom onchange="onSaleNomChange(this)">${opts}</select></td>
                <td><input type="number" data-qty value="1" min="0.001" step="0.001" oninput="recalcSaleTotal()" /></td>
                <td><input type="number" data-price value="0" min="0" step="0.01" oninput="recalcSaleTotal()" /></td>
                <td><button class="btn btn-sm red" onclick="removeSaleRow(this)">✕</button></td>`;
            tb.appendChild(tr);
            const sel = tr.querySelector('select[data-nom]');
            if (sel && sel.selectedOptions.length) tr.querySelector('input[data-price]').value = sel.selectedOptions[0].dataset.price || 0;
            recalcSaleTotal();
        }
        function onSaleNomChange(sel) {
            const tr = sel.closest('tr');
            if (sel.selectedOptions.length) tr.querySelector('input[data-price]').value = sel.selectedOptions[0].dataset.price || 0;
            recalcSaleTotal();
        }
        function removeSaleRow(btn) { btn.closest('tr').remove(); recalcSaleTotal(); }
        function recalcSaleTotal() {
            let total = 0;
            document.querySelectorAll('#new-sale-items tr').forEach(tr => {
                const q = parseFloat(tr.querySelector('input[data-qty]').value) || 0;
                const p = parseFloat(tr.querySelector('input[data-price]').value) || 0;
                total += q * p;
            });
            document.getElementById('new-sale-total').textContent = 'Итого: ' + total.toFixed(2);
        }
        async function createSale() {
            try {
                const rows = document.querySelectorAll('#new-sale-items tr');
                if (!rows.length) throw new Error('Добавьте хотя бы одну строку в табличную часть');
                const orgId = parseInt(document.getElementById('new-sale-org').value);
                const cpId = parseInt(document.getElementById('new-sale-cp').value);
                if (!orgId || !cpId) throw new Error('Выберите организацию и контрагента');
                const items = [];
                rows.forEach(tr => {
                    items.push({
                        nomenclature_id: parseInt(tr.querySelector('select[data-nom]').value),
                        quantity: parseFloat(tr.querySelector('input[data-qty]').value) || 0,
                        price: parseFloat(tr.querySelector('input[data-price]').value) || 0
                    });
                });
                const d = await postJson('/api/sales', {
                    number: document.getElementById('new-sale-number').value.trim(),
                    date: document.getElementById('new-sale-date').value || undefined,
                    organization_id: orgId,
                    counterparty_id: cpId,
                    comment: document.getElementById('new-sale-comment').value,
                    items
                });
                setStatus('new-sale-status', true, `Документ ${d.number} сохранён на сумму ${d.total_sum}`);
                document.getElementById('new-sale-number').value = '';
                document.getElementById('new-sale-items').innerHTML = '';
                addSaleRow();
                loadSales();
            } catch(e) { setStatus('new-sale-status', false, e.message); }
        }

        // ============================================================
        // 6. ОТПРАВКА В 1С
        // ============================================================
        let _s1c = null;
        function toggleAuth1c() {
            const use = document.getElementById('s1c-use-auth').value === '1';
            document.querySelectorAll('.auth1c-row').forEach(el => el.style.display = use ? '' : 'none');
        }
        async function loadSettings1c() {
            const d = await fetchData('/api/settings_1c');
            if (d.error) return;
            _s1c = d;
            document.getElementById('s1c-base-url').value = d.base_url || '';
            document.getElementById('s1c-timeout').value = d.timeout || 10;
            document.getElementById('s1c-verify').value = d.verify_ssl ? '1' : '0';
            document.getElementById('s1c-use-auth').value = d.use_auth ? '1' : '0';
            document.getElementById('s1c-login').value = d.login || '';
            document.getElementById('s1c-password').value = '';
            document.getElementById('s1c-password').placeholder = d.has_password ? '*** (сохранён, введите новый чтобы заменить)' : 'пароль не задан';
            document.getElementById('s1c-method').value = d.default_method || 'POST';
            const ep = d.endpoints || {};
            document.getElementById('s1c-ep-organizations').value = ep.organizations || '';
            document.getElementById('s1c-ep-counterparties').value = ep.counterparties || '';
            document.getElementById('s1c-ep-units').value = ep.units || '';
            document.getElementById('s1c-ep-nomenclatures').value = ep.nomenclatures || '';
            document.getElementById('s1c-ep-sales').value = ep.sales || '';
            toggleAuth1c();
            // подставить путь в отправителе, если пусто
            if (!document.getElementById('send-path').value) onEntityChange();
        }
        async function saveSettings1c() {
            const st = document.getElementById('s1c-settings-status');
            st.textContent = '⏳ Сохранение...';
            const payload = {
                base_url: document.getElementById('s1c-base-url').value.trim(),
                timeout: parseInt(document.getElementById('s1c-timeout').value) || 10,
                verify_ssl: document.getElementById('s1c-verify').value === '1',
                use_auth: document.getElementById('s1c-use-auth').value === '1',
                login: document.getElementById('s1c-login').value.trim(),
                default_method: document.getElementById('s1c-method').value,
                endpoints: {
                    organizations: document.getElementById('s1c-ep-organizations').value.trim(),
                    counterparties: document.getElementById('s1c-ep-counterparties').value.trim(),
                    units: document.getElementById('s1c-ep-units').value.trim(),
                    nomenclatures: document.getElementById('s1c-ep-nomenclatures').value.trim(),
                    sales: document.getElementById('s1c-ep-sales').value.trim()
                }
            };
            const pwd = document.getElementById('s1c-password').value;
            if (pwd && pwd !== '***') payload.password = pwd;
            try {
                const r = await fetch('/api/settings_1c', { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
                const d = await r.json();
                if (!r.ok) throw new Error(d.error || 'Ошибка');
                _s1c = d;
                st.innerHTML = '<span class="ok-text">✅ Сохранено</span>';
                onEntityChange();
            } catch(e) { st.innerHTML = '<span class="err-text">❌ ' + e.message + '</span>'; }
        }
        async function testConnection1c() {
            const st = document.getElementById('s1c-settings-status');
            st.textContent = '⏳ Проверка...';
            try {
                const r = await fetch('/api/test_1c', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ base_url: document.getElementById('s1c-base-url').value.trim() }) });
                const d = await r.json();
                if (d.success) st.innerHTML = `<span class="ok-text">✅ Связь есть (HTTP ${d.status})</span>`;
                else st.innerHTML = `<span class="err-text">❌ Нет связи: ${d.error || ('HTTP ' + d.status)} — ${((d.body||'').substring(0,200))}</span>`;
            } catch(e) { st.innerHTML = '<span class="err-text">❌ ' + e.message + '</span>'; }
        }
        const ENTITY_API = { organizations: '/api/organizations', counterparties: '/api/counterparties', units: '/api/units', nomenclatures: '/api/nomenclatures', sales: '/api/sales' };
        function onEntityChange() {
            const ent = document.getElementById('send-entity').value;
            const isCustom = (ent === 'custom');
            document.getElementById('send-record').disabled = isCustom;
            // путь по умолчанию из настроек
            if (_s1c && _s1c.endpoints && _s1c.endpoints[ent]) {
                document.getElementById('send-path').value = _s1c.endpoints[ent];
            }
            if (_s1c && _s1c.default_method) document.getElementById('send-method').value = _s1c.default_method;
            if (isCustom) {
                document.getElementById('send-path').value = document.getElementById('send-path').value || '/custom';
                if (!document.getElementById('send-body').value) document.getElementById('send-body').value = JSON.stringify({hello: '1C'}, null, 2);
            } else {
                loadRecordsForSend();
            }
        }
        async function loadRecordsForSend() {
            const ent = document.getElementById('send-entity').value;
            const sel = document.getElementById('send-record');
            if (ent === 'custom') { sel.innerHTML = '<option value="">—</option>'; return; }
            const d = await fetchData(ENTITY_API[ent]);
            if (!d || d.error || !d.length) { sel.innerHTML = '<option value="">(нет записей)</option>'; return; }
            sel.innerHTML = d.map(it => {
                let label = '#' + it.id;
                if (it.code) label += ' [' + it.code + ']';
                if (it.name) label += ' — ' + it.name;
                else if (it.number) label += ' — ' + it.number;
                return `<option value="${it.id}">${label}</option>`;
            }).join('');
            // сразу подставить тело первой записи для удобства
            buildBodyPreview();
        }
        async function buildBodyPreview() {
            const ent = document.getElementById('send-entity').value;
            if (ent === 'custom') return;
            const rid = document.getElementById('send-record').value;
            if (!rid) { document.getElementById('send-body').value = ''; return; }
            const d = await fetchData(`/api/build_1c_body?entity=${ent}&record_id=${rid}`);
            if (d.error) { alert('❌ ' + d.error); return; }
            document.getElementById('send-body').value = JSON.stringify(d, null, 2);
        }
        async function sendTo1c() {
            const st = document.getElementById('send-status');
            const ent = document.getElementById('send-entity').value;
            const method = document.getElementById('send-method').value;
            const path = document.getElementById('send-path').value.trim();
            let body = null;
            const raw = document.getElementById('send-body').value.trim();
            if (raw && method !== 'GET' && method !== 'DELETE') {
                try { body = JSON.parse(raw); }
                catch(e) { st.innerHTML = '<span class="err-text">❌ Тело — невалидный JSON: ' + e.message + '</span>'; return; }
            }
            const payload = { entity: ent, method, path, body };
            if (ent !== 'custom') payload.record_id = parseInt(document.getElementById('send-record').value) || null;
            // если тело пустое, но выбрана запись — бэкенд сам соберёт из БД
            st.textContent = '⏳ Отправка...';
            try {
                const r = await fetch('/api/send_to_1c', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
                const d = await r.json();
                if (d.success) st.innerHTML = `<span class="ok-text">✅ Отправлено → ${d.method} ${d.url} (HTTP ${d.status})</span>`;
                else st.innerHTML = `<span class="err-text">❌ Ошибка (HTTP ${d.status ?? '—'}): ${d.error || ('' + (d.response||'').substring(0,300))}</span>`;
                loadLog();
            } catch(e) { st.innerHTML = '<span class="err-text">❌ ' + e.message + '</span>'; }
        }
        async function quickSendTo1c(entity, id) {
            if (!confirm(`Отправить запись #${id} (${entity}) в 1С?`)) return;
            try {
                // тянем актуальные настройки, чтобы взять правильный путь
                if (!_s1c) { const s = await fetchData('/api/settings_1c'); if (!s.error) _s1c = s; }
                const path = (_s1c && _s1c.endpoints && _s1c.endpoints[entity]) || ('/' + entity);
                const method = (_s1c && _s1c.default_method) || 'POST';
                const r = await fetch('/api/send_to_1c', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ entity, record_id: id, method, path }) });
                const d = await r.json();
                if (d.success) alert(`✅ Отправлено в 1С\\n${d.method} ${d.url}\\nHTTP ${d.status}`);
                else alert(`❌ Ошибка отправки\\n${d.error || d.response || ('HTTP ' + d.status)}\\n${d.url}`);
            } catch(e) { alert('❌ ' + e.message); }
        }
        async function loadLog() {
            const box = document.getElementById('log-list');
            if (!box) return;
            const d = await fetchData('/api/outbox_log?limit=100');
            if (!d || d.error) { box.innerHTML = `<div class="status error">❌ ${d?.error||'Ошибка'}</div>`; return; }
            if (!d.length) { box.innerHTML = `<div class="status empty">📭 Пока ничего не отправляли</div>`; return; }
            let html = '<table><thead><tr><th>ID</th><th>Время</th><th>Сущность</th><th>Метод</th><th>URL</th><th>Статус</th><th></th><th></th></tr></thead><tbody>';
            d.forEach(l => {
                const tag = l.success ? '<span class="tag-ok">OK</span>' : '<span class="tag-fail">FAIL</span>';
                const shortUrl = (l.url || '').length > 45 ? (l.url.substring(0,45) + '...') : (l.url || '');
                html += `<tr><td>${l.id}</td><td>${(l.created_at||'').substring(0,19).replace('T',' ')}</td><td>${l.entity||''} ${l.record_id??''}</td><td>${l.method||''}</td><td title="${l.url||''}">${shortUrl}</td><td>${l.response_status ?? '—'} ${tag}</td><td><button class="btn btn-sm" onclick="viewLogDetail(${l.id})">📄</button></td><td><button class="btn btn-sm red" onclick="deleteLogItem(${l.id})">🗑️</button></td></tr>`;
            });
            html += '</tbody></table>';
            box.innerHTML = html;
            window._logCache = {};
            d.forEach(l => window._logCache[l.id] = l);
        }
        function viewLogDetail(id) {
            const l = window._logCache && window._logCache[id];
            if (!l) return;
            document.getElementById('modal-title').textContent = `📄 Отправка #${l.id} → ${l.url}`;
            document.getElementById('modal-body').innerHTML = `
                <div class="doc-detail-header">
                    <div><strong>Время:</strong> ${(l.created_at||'').substring(0,19).replace('T',' ')}</div>
                    <div><strong>Метод:</strong> ${l.method}</div>
                    <div><strong>HTTP:</strong> ${l.response_status ?? '—'}</div>
                    <div><strong>Успех:</strong> ${l.success ? '✅' : '❌'}</div>
                    ${l.error ? `<div><strong>Ошибка:</strong> ${l.error}</div>` : ''}
                </div>
                <h4>Запрос</h4><div class="json-view">${(l.request_body||'(пусто)').substring(0,4000)}</div>
                <h4 style="margin-top:10px;">Ответ 1С</h4><div class="json-view">${(l.response_body||'(пусто)').substring(0,4000)}</div>
            `;
            document.getElementById('modal').style.display = 'flex';
        }
        async function deleteLogItem(id) {
            if (!confirm('Удалить запись журнала?')) return;
            await fetch('/api/outbox_log/' + id, { method: 'DELETE' });
            loadLog();
        }
        async function clearLog() {
            if (!confirm('Очистить весь журнал отправок?')) return;
            await fetch('/api/outbox_log', { method: 'DELETE' });
            loadLog();
        }

        // ============================================================
        // 7. ЗАГРУЗКА ПРИ СТАРТЕ
        // ============================================================
        window.onload = function() {
            loadOrgs();
            loadCounterparties();
            loadUnits();
            loadNomenclatures();
            loadSettings1c();
        };
    </script>
</body>
</html>
    ''')

# ---------- СОЗДАНИЕ ТАБЛИЦ + МИГРАЦИЯ КОДОВ ----------
with app.app_context():
    db.create_all()
    ensure_code_columns()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)