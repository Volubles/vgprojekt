import os
import json

from flask import Flask, render_template, request, redirect, url_for, session
import psycopg2
from psycopg2.extras import RealDictCursor
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "byt-ut-detta-hemliga-nyckeln")

DB_CONFIG = {
    "dbname": os.getenv("DB_NAME", "postgres"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", "root"),
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "5432"),
    "options": os.getenv("DB_OPTIONS", "-c lc_messages=C"),
}


def get_db_connection():
    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
    conn.set_client_encoding("UTF8")
    return conn


# ── Index ──────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    db_error = None
    kunder = []
    meny = {}
    receipt = None

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT * FROM Kunder;")
        kunder = cur.fetchall()

        cur.execute("""
            SELECT p.ProduktID, p.ProduktNamn, p.Pris, pt.ProduktTypNamn
            FROM Produkter p
            JOIN Produkttyp pt ON p.ProduktTypID = pt.ProduktTypID
            ORDER BY pt.ProduktTypID, p.ProduktID;
        """)
        produkter_db = cur.fetchall()

        meny = {}
        for p in produkter_db:
            kat = p['produkttypnamn']
            if kat not in meny:
                meny[kat] = []
            meny[kat].append(p)

        last_id = request.args.get('last_id')
        if last_id:
            cur.execute("SELECT * FROM OrdersammanFattning WHERE kvittonummer = %s;", (last_id,))
            rows = cur.fetchall()
            if rows:
                receipt = {
                    "receiptno":    rows[0]['kvittonummer'],
                    "orderdate":    rows[0]['orderdatum'],
                    "customername": rows[0]['kundnamn'],
                    "finaltotal":   rows[0]['totalbelopp'],
                    "itemlist": [
                        {"name": r['produktnamn'], "qty": r['antal'], "price": float(r['pris'])}
                        for r in rows
                    ]
                }

        cur.close()
        conn.close()
    except Exception as e:
        db_error = str(e)

    return render_template('index.html',
                           kunder=kunder,
                           meny=meny,
                           receipt=receipt,
                           error_login=request.args.get('error_login'),
                           error_register=request.args.get('error_register'),
                           db_error=db_error)


# ── Place Order ────────────────────────────────────────────────────────────────
@app.route('/order', methods=['POST'])
def place_order():
    kund_id = session.get('kund_id')
    if not kund_id:
        return redirect(url_for('index', error_login="Logga in för att beställa."))

    cart_data = request.form.get('cart_data')
    if not cart_data:
        return redirect(url_for('index'))

    try:
        cart = json.loads(cart_data)
    except Exception:
        return redirect(url_for('index'))

    bestallning_rader = [
        {"produkt_id": int(k), "antal": int(v['qty'])}
        for k, v in cart.items() if int(v['qty']) > 0
    ]

    if not bestallning_rader:
        return redirect(url_for('index'))

    conn = get_db_connection()
    cur = conn.cursor()
    order_id = None

    try:
        totalbelopp = 0
        for rad in bestallning_rader:
            cur.execute("SELECT Pris FROM Produkter WHERE ProduktID = %s;", (rad['produkt_id'],))
            p = cur.fetchone()
            if p:
                totalbelopp += p['pris'] * rad['antal']

        cur.execute("SELECT COALESCE(MAX(OrderID), 1000) + 1 AS next_id FROM Bestallningar;")
        order_id = cur.fetchone()['next_id']

        cur.execute(
            "INSERT INTO Bestallningar (OrderID, KundID, Totalbelopp) VALUES (%s, %s, %s);",
            (order_id, kund_id, totalbelopp)
        )
        for rad in bestallning_rader:
            cur.execute(
                "INSERT INTO Orderrader (OrderID, ProduktID, Antal) VALUES (%s, %s, %s);",
                (order_id, rad['produkt_id'], rad['antal'])
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Databasfel: {e}")
        order_id = None
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('index', last_id=order_id) if order_id else url_for('index'))


# ── Register ───────────────────────────────────────────────────────────────────
@app.route('/register', methods=['POST'])
def register():
    namn          = request.form['namn']
    email         = request.form['email']
    telefon       = request.form['telefon']
    adress        = request.form['adress']
    losenord_hash = generate_password_hash(request.form['losenord'])

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO Kunder (Namn, Email, Telefon, Adress, Losenord)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING KundID, Namn;
        """, (namn, email, telefon, adress, losenord_hash))
        row = cur.fetchone()
        conn.commit()
        session['kund_id'] = row['kundid']
        session['namn']    = row['namn']
        return redirect(url_for('index'))
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return redirect(url_for('index', error_register="Email redan registrerad."))
    except Exception as e:
        conn.rollback()
        return redirect(url_for('index', error_register=str(e)))
    finally:
        cur.close()
        conn.close()


# ── Login ──────────────────────────────────────────────────────────────────────
@app.route('/login', methods=['POST'])
def login():
    email    = request.form['email']
    losenord = request.form['losenord']

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT KundID, Namn, Losenord FROM Kunder WHERE Email = %s;",
            (email.lower(),)
        )
        row = cur.fetchone()

        if row and check_password_hash(row['losenord'], losenord):
            session['kund_id'] = row['kundid']
            session['namn']    = row['namn']
            return redirect(url_for('index'))
        else:
            return redirect(url_for('index', error_login="Fel email eller lösenord."))
    except Exception as e:
        return redirect(url_for('index', error_login=str(e)))
    finally:
        cur.close()
        conn.close()


# ── Logout ─────────────────────────────────────────────────────────────────────
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(debug=True)
