import json
import os
import re
import threading
from decimal import Decimal, InvalidOperation

from flask import Flask, redirect, render_template, request, session, url_for
import psycopg2
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__, template_folder=".", static_folder="static", static_url_path="/static")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "byt-ut-detta-hemliga-nyckeln")

STAFF_ROLE_ADMIN = "admin"
STAFF_ROLE_EMPLOYEE = "anstalld"

DEFAULT_PRODUCTS = [
    {
        "type_id": 1,
        "name": "Vesuvio",
        "price": Decimal("85.00"),
        "description": "Tomatsas, ost och skinka. En klassisk favorit för den som vill ha det enkelt och gott.",
        "stock": 100,
    },
    {
        "type_id": 1,
        "name": "Bussola",
        "price": Decimal("90.00"),
        "description": "Tomatsas, ost, skinka och räkor. En mild pizza med lite extra lyx.",
        "stock": 100,
    },
    {
        "type_id": 1,
        "name": "Banana",
        "price": Decimal("95.00"),
        "description": "Tomatsas, ost, skinka, banan och curry. Sötma och krydda i samma pizza.",
        "stock": 100,
    },
    {
        "type_id": 1,
        "name": "Tropical",
        "price": Decimal("95.00"),
        "description": "Tomatsas, ost, skinka och ananas. Frisk, söt och fortfarande klassiskt pizzeria.",
        "stock": 100,
    },
    {
        "type_id": 1,
        "name": "Quattro Stagioni",
        "price": Decimal("100.00"),
        "description": "Fyra smaker på samma pizza med skinka, champinjoner, räkor och musslor.",
        "stock": 100,
    },
    {
        "type_id": 2,
        "name": "Vitlokssås",
        "price": Decimal("15.00"),
        "description": "Krämig vitlokssås som passar till pizza, pommes och sallad.",
        "stock": 100,
    },
    {
        "type_id": 2,
        "name": "Bearnaisesås",
        "price": Decimal("15.00"),
        "description": "Klassisk bea med tydlig dragon och krämig konsistens.",
        "stock": 100,
    },
    {
        "type_id": 2,
        "name": "Coca Cola",
        "price": Decimal("25.00"),
        "description": "Kall 33 cl lask som passar till hela menyn.",
        "stock": 100,
    },
    {
        "type_id": 2,
        "name": "Fanta",
        "price": Decimal("25.00"),
        "description": "Apelsinlask 33 cl för den som vill ha något fruktigare.",
        "stock": 100,
    },
]

DB_CONFIG = {
    "dbname": os.getenv("DB_NAME", "postgres"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", "root"),
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "5432"),
    "options": os.getenv("DB_OPTIONS", "-c lc_messages=C"),
}
DB_CLIENT_ENCODING = os.getenv("DB_CLIENT_ENCODING", "").strip()

_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()


def get_bootstrap_admin_username():
    value = normalize_text(os.getenv("ADMIN_USERNAME"))
    return value or "admin"


def get_bootstrap_admin_password():
    value = normalize_text(os.getenv("ADMIN_PASSWORD"))
    return value or "admin123"


def format_db_error(exc):
    if isinstance(exc, UnicodeDecodeError):
        raw_message = exc.object
        if isinstance(raw_message, (bytes, bytearray)):
            decoded = raw_message.decode("latin-1", errors="replace")
            lowered = decoded.lower()
            if "losenordsautentisering misslyckades" in lowered or "password authentication failed" in lowered:
                return (
                    "Databasinloggningen misslyckades. Kontrollera DB_USER och DB_PASSWORD "
                    "i din miljo eller i appens standardvarden."
                )
            return decoded
    return str(exc)


def format_stock_error(cur, produkt_id, antal, lagersaldo):
    """Returnerar ett användarvänligt felmeddelande när en produkt är slut i lager."""
    cur.execute(
        "SELECT ProduktNamn FROM Produkter WHERE ProduktID = %s;",
        (produkt_id,),
    )
    row = cur.fetchone()
    produktnamn = row["produktnamn"] if row else f"Produkt {produkt_id}"
    if lagersaldo == 0:
        return (
            f"Tyvärr är {produktnamn} slut i lager. "
            "Ta bort den från varukorgen och försök igen."
        )
    if antal is not None:
        return (
            f"Tyvärr finns det bara {lagersaldo} st {produktnamn} kvar i lager "
            f"(du försökte beställa {antal}). Ta bort eller minska antalet i varukorgen och försök igen."
        )
    return (
        f"Tyvärr finns det bara {lagersaldo} st {produktnamn} kvar i lager. "
        "Ta bort eller minska antalet i varukorgen och försök igen."
    )


def get_db_connection():
    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
    if DB_CLIENT_ENCODING:
        conn.set_client_encoding(DB_CLIENT_ENCODING)
    return conn


def get_ready_connection():
    ensure_database_schema()
    return get_db_connection()


def normalize_text(value):
    return (value or "").strip()


def normalize_email(value):
    return normalize_text(value).lower()


def normalize_checkbox(value):
    return str(value).lower() in {"1", "true", "yes", "on"}


def safe_rollback(conn):
    try:
        if conn:
            conn.rollback()
    except Exception:
        pass


def clear_customer_session():
    session.pop("kund_id", None)
    session.pop("kund_namn", None)


def clear_staff_session():
    session.pop("anstalld_id", None)
    session.pop("anstalld_namn", None)
    session.pop("anstalld_anvandarnamn", None)
    session.pop("anstalld_roll", None)


def set_customer_session(row):
    session["kund_id"] = row["kundid"]
    session["kund_namn"] = row["namn"]


def set_staff_session(row):
    session["anstalld_id"] = row["anstalldid"]
    session["anstalld_namn"] = row["namn"]
    session["anstalld_anvandarnamn"] = row["anvandarnamn"]
    session["anstalld_roll"] = row["roll"]


def role_label(role):
    if role == STAFF_ROLE_ADMIN:
        return "Admin"
    return "Anstalld"


def parse_price(value):
    try:
        price = Decimal(normalize_text(value).replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("Priset måste vara ett giltigt nummer.") from exc

    if price <= 0:
        raise ValueError("Priset måste vara större än 0.")
    return price


def parse_non_negative_int(value, field_label):
    try:
        parsed = int(normalize_text(value))
    except ValueError as exc:
        raise ValueError(f"{field_label} måste vara ett heltal.") from exc

    if parsed < 0:
        raise ValueError(f"{field_label} kan inte vara negativt.")
    return parsed


def get_logged_in_customer(cur):
    kund_id = session.get("kund_id")
    if not kund_id:
        return None

    cur.execute(
        """
        SELECT KundID, Namn, Email, Telefon, Adress
        FROM Kunder
        WHERE KundID = %s;
        """,
        (kund_id,),
    )
    return cur.fetchone()


def get_logged_in_staff(cur):
    anstalld_id = session.get("anstalld_id")
    if not anstalld_id:
        return None

    cur.execute(
        """
        SELECT
            AnstalldID,
            Anvandarnamn,
            Namn,
            Email,
            Telefon,
            Roll,
            Aktiv,
            SkapadAv,
            SkapadDatum,
            UppdateradDatum
        FROM Anstallda
        WHERE AnstalldID = %s;
        """,
        (anstalld_id,),
    )
    row = cur.fetchone()
    if row and row["aktiv"]:
        return row
    return None


def staff_has_role(*roles):
    staff_id = session.get("anstalld_id")
    staff_role = session.get("anstalld_roll")
    if not staff_id:
        return False
    if roles and staff_role not in roles:
        return False
    return True


def fetch_staff_by_identifier(cur, identifier):
    cur.execute(
        """
        SELECT
            AnstalldID,
            Anvandarnamn,
            Namn,
            Email,
            Telefon,
            Roll,
            Aktiv,
            Losenord,
            SkapadAv,
            SkapadDatum,
            UppdateradDatum
        FROM Anstallda
        WHERE LOWER(Anvandarnamn) = LOWER(%s)
           OR LOWER(Email) = LOWER(%s);
        """,
        (identifier, identifier),
    )
    return cur.fetchone()


def fetch_menu(cur):
    cur.execute(
        """
        SELECT
            p.ProduktID,
            p.ProduktNamn,
            p.Pris,
            COALESCE(p.Beskrivning, '') AS Beskrivning,
            pt.ProduktTypID,
            pt.ProduktTypNamn
        FROM Produkter p
        JOIN Produkttyp pt ON p.ProduktTypID = pt.ProduktTypID
        ORDER BY pt.ProduktTypID, p.ProduktID;
        """
    )

    meny = {}
    for produkt in cur.fetchall():
        kategori = produkt["produkttypnamn"]
        meny.setdefault(kategori, []).append(produkt)
    return meny


def fetch_product_types(cur):
    cur.execute(
        """
        SELECT ProduktTypID, ProduktTypNamn
        FROM Produkttyp
        ORDER BY ProduktTypID;
        """
    )
    return cur.fetchall()


def fetch_pizza_products(cur):
    cur.execute(
        """
        SELECT
            p.ProduktID,
            p.ProduktNamn,
            p.Pris,
            COALESCE(p.Beskrivning, '') AS Beskrivning,
            p.LagerSaldo,
            pt.ProduktTypNamn
        FROM Produkter p
        JOIN Produkttyp pt ON p.ProduktTypID = pt.ProduktTypID
        WHERE LOWER(pt.ProduktTypNamn) = 'pizza'
        ORDER BY p.ProduktNamn;
        """
    )
    return cur.fetchall()


def fetch_staff_products(cur):
    """Produkter som anställda får redigera: pizzor och tillbehör (Läsk & sås)."""
    cur.execute(
        """
        SELECT
            p.ProduktID,
            p.ProduktNamn,
            p.Pris,
            COALESCE(p.Beskrivning, '') AS Beskrivning,
            p.LagerSaldo,
            pt.ProduktTypNamn
        FROM Produkter p
        JOIN Produkttyp pt ON p.ProduktTypID = pt.ProduktTypID
        WHERE pt.ProduktTypID IN (1, 2)
        ORDER BY pt.ProduktTypID, p.ProduktNamn;
        """
    )
    return cur.fetchall()


def fetch_admin_dashboard_data(cur, start_datum="", slut_datum=""):
    data = {
        "counts": {},
        "kunder": [],
        "anstallda": [],
        "produkter": [],
        "produkttyper": [],
        "bestallningar": [],
        "orderrader": [],
        "kundlogg": [],
        "ordersammanfattning": [],
        "sales": None,
        "db_name": DB_CONFIG["dbname"],
    }

    cur.execute("SELECT COUNT(*) AS n FROM Kunder;")
    data["counts"]["kunder"] = cur.fetchone()["n"]

    cur.execute("SELECT COUNT(*) AS n FROM Anstallda;")
    data["counts"]["anstallda"] = cur.fetchone()["n"]

    cur.execute("SELECT COUNT(*) AS n FROM Produkter;")
    data["counts"]["produkter"] = cur.fetchone()["n"]

    cur.execute("SELECT COUNT(*) AS n FROM Bestallningar;")
    data["counts"]["bestallningar"] = cur.fetchone()["n"]

    cur.execute("SELECT COUNT(*) AS n FROM Orderrader;")
    data["counts"]["orderrader"] = cur.fetchone()["n"]

    cur.execute(
        """
        SELECT KundID, Namn, Email, Telefon, Adress
        FROM Kunder
        ORDER BY KundID;
        """
    )
    data["kunder"] = cur.fetchall()

    cur.execute(
        """
        SELECT
            AnstalldID,
            Anvandarnamn,
            Namn,
            Email,
            Telefon,
            Roll,
            Aktiv,
            SkapadAv,
            SkapadDatum,
            UppdateradDatum
        FROM Anstallda
        ORDER BY CASE WHEN Roll = 'admin' THEN 0 ELSE 1 END, Namn;
        """
    )
    data["anstallda"] = cur.fetchall()

    cur.execute(
        """
        SELECT
            p.ProduktID,
            p.ProduktNamn,
            p.Pris,
            p.LagerSaldo,
            COALESCE(p.Beskrivning, '') AS Beskrivning,
            pt.ProduktTypID,
            pt.ProduktTypNamn
        FROM Produkter p
        JOIN Produkttyp pt ON p.ProduktTypID = pt.ProduktTypID
        ORDER BY pt.ProduktTypID, p.ProduktID;
        """
    )
    data["produkter"] = cur.fetchall()
    data["produkttyper"] = fetch_product_types(cur)

    cur.execute(
        """
        SELECT OrderID, KundID, Totalbelopp, Datum, Leveransadress
        FROM Bestallningar
        ORDER BY Datum DESC, OrderID DESC;
        """
    )
    data["bestallningar"] = cur.fetchall()

    cur.execute(
        """
        SELECT OrderradID, OrderID, ProduktID, Antal
        FROM Orderrader
        ORDER BY OrderradID DESC;
        """
    )
    data["orderrader"] = cur.fetchall()

    cur.execute(
        """
        SELECT LoggID, KundID, Handelse, Loggdatum
        FROM Kundlogg
        ORDER BY Loggdatum DESC, LoggID DESC;
        """
    )
    data["kundlogg"] = cur.fetchall()

    cur.execute(
        """
        SELECT *
        FROM OrdersammanFattning
        ORDER BY KvittoNummer DESC, ProduktNamn;
        """
    )
    data["ordersammanfattning"] = cur.fetchall()

    if start_datum and slut_datum:
        cur.execute("CALL BeraknaTotalForsaljning(%s, %s);", (start_datum, slut_datum))
        cur.execute(
            """
            SELECT COALESCE(SUM(Totalbelopp), 0) AS total
            FROM Bestallningar
            WHERE Datum::DATE BETWEEN %s AND %s;
            """,
            (start_datum, slut_datum),
        )
        data["sales"] = {
            "start": start_datum,
            "end": slut_datum,
            "total": float(cur.fetchone()["total"]),
        }

    return data


def ensure_database_schema():
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return

    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return

        conn = get_db_connection()
        cur = conn.cursor()

        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS Kunder (
                    KundID SERIAL PRIMARY KEY,
                    Namn VARCHAR(100) NOT NULL,
                    Email VARCHAR(255) UNIQUE NOT NULL,
                    Telefon VARCHAR(30),
                    Adress VARCHAR(255),
                    Losenord VARCHAR(255) NOT NULL,
                    SkapadDatum TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS Kundlogg (
                    LoggID SERIAL PRIMARY KEY,
                    KundID INT REFERENCES Kunder(KundID) ON DELETE SET NULL,
                    Handelse VARCHAR(255) NOT NULL,
                    Loggdatum TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS Produkttyp (
                    ProduktTypID INT PRIMARY KEY,
                    ProduktTypNamn VARCHAR(100) NOT NULL UNIQUE
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS Produkter (
                    ProduktID SERIAL PRIMARY KEY,
                    ProduktTypID INT NOT NULL REFERENCES Produkttyp(ProduktTypID),
                    ProduktNamn VARCHAR(60) NOT NULL UNIQUE,
                    Pris NUMERIC(14, 2) NOT NULL CHECK (Pris > 0),
                    Beskrivning TEXT NOT NULL DEFAULT '',
                    LagerSaldo INT NOT NULL DEFAULT 0 CHECK (LagerSaldo >= 0)
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS Bestallningar (
                    OrderID INT PRIMARY KEY,
                    KundID INT NOT NULL REFERENCES Kunder(KundID),
                    Totalbelopp NUMERIC(10, 2) NOT NULL CHECK (Totalbelopp > 0),
                    Datum TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    Leveransadress VARCHAR(255)
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS Orderrader (
                    OrderradID SERIAL PRIMARY KEY,
                    OrderID INT NOT NULL REFERENCES Bestallningar(OrderID),
                    ProduktID INT NOT NULL REFERENCES Produkter(ProduktID),
                    Antal INT NOT NULL CHECK (Antal > 0)
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS Anstallda (
                    AnstalldID SERIAL PRIMARY KEY,
                    Anvandarnamn VARCHAR(50) NOT NULL UNIQUE,
                    Namn VARCHAR(100) NOT NULL,
                    Email VARCHAR(255) NOT NULL UNIQUE,
                    Telefon VARCHAR(30),
                    Roll VARCHAR(20) NOT NULL DEFAULT 'anstalld',
                    Losenord VARCHAR(255) NOT NULL,
                    Aktiv BOOLEAN NOT NULL DEFAULT TRUE,
                    SkapadAv INT REFERENCES Anstallda(AnstalldID) ON DELETE SET NULL,
                    SkapadDatum TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UppdateradDatum TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            cur.execute("ALTER TABLE Kunder ALTER COLUMN Telefon DROP NOT NULL;")
            cur.execute("ALTER TABLE Kunder ALTER COLUMN Adress DROP NOT NULL;")
            cur.execute(
                "ALTER TABLE Kunder ADD COLUMN IF NOT EXISTS Losenord VARCHAR(255) NOT NULL DEFAULT '';"
            )
            cur.execute("ALTER TABLE Produkter ADD COLUMN IF NOT EXISTS Beskrivning TEXT NOT NULL DEFAULT '';")
            cur.execute(
                "ALTER TABLE Produkter ADD COLUMN IF NOT EXISTS LagerSaldo INT NOT NULL DEFAULT 0;"
            )
            cur.execute(
                "ALTER TABLE Bestallningar ADD COLUMN IF NOT EXISTS Leveransadress VARCHAR(255);"
            )
            cur.execute("ALTER TABLE Anstallda ADD COLUMN IF NOT EXISTS Telefon VARCHAR(30);")
            cur.execute(
                "ALTER TABLE Anstallda ADD COLUMN IF NOT EXISTS Aktiv BOOLEAN NOT NULL DEFAULT TRUE;"
            )
            cur.execute(
                "ALTER TABLE Anstallda ADD COLUMN IF NOT EXISTS SkapadAv INT REFERENCES Anstallda(AnstalldID) ON DELETE SET NULL;"
            )
            cur.execute(
                "ALTER TABLE Anstallda ADD COLUMN IF NOT EXISTS SkapadDatum TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;"
            )
            cur.execute(
                "ALTER TABLE Anstallda ADD COLUMN IF NOT EXISTS UppdateradDatum TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;"
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bestallningar_kundid
                ON Bestallningar (KundID);
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bestallningar_datum
                ON Bestallningar (Datum);
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_orderrader_orderid
                ON Orderrader (OrderID);
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_produkter_typ
                ON Produkter (ProduktTypID);
                """
            )

            cur.execute(
                """
                CREATE OR REPLACE FUNCTION uppdatera_lager_vid_order()
                RETURNS TRIGGER AS $$
                BEGIN
                    UPDATE Produkter
                    SET LagerSaldo = LagerSaldo - NEW.Antal
                    WHERE ProduktID = NEW.ProduktID
                      AND LagerSaldo >= NEW.Antal;

                    IF NOT FOUND THEN
                        RAISE EXCEPTION 'Otillrackligt lager for produkt %', NEW.ProduktID;
                    END IF;

                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
            cur.execute("DROP TRIGGER IF EXISTS trigger_uppdatera_lager ON Orderrader;")
            cur.execute(
                """
                CREATE TRIGGER trigger_uppdatera_lager
                AFTER INSERT ON Orderrader
                FOR EACH ROW
                EXECUTE FUNCTION uppdatera_lager_vid_order();
                """
            )

            cur.execute(
                """
                CREATE OR REPLACE FUNCTION logga_ny_kund()
                RETURNS TRIGGER AS $$
                BEGIN
                    INSERT INTO Kundlogg (KundID, Handelse)
                    VALUES (NEW.KundID, 'Ny kund registrerad');
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
            cur.execute("DROP TRIGGER IF EXISTS trigger_logga_ny_kund ON Kunder;")
            cur.execute(
                """
                CREATE TRIGGER trigger_logga_ny_kund
                AFTER INSERT ON Kunder
                FOR EACH ROW
                EXECUTE FUNCTION logga_ny_kund();
                """
            )

            cur.execute(
                """
                CREATE OR REPLACE FUNCTION logga_kund_uppdatering()
                RETURNS TRIGGER AS $$
                BEGIN
                    INSERT INTO Kundlogg (KundID, Handelse)
                    VALUES (NEW.KundID, 'Kundprofil uppdaterad');
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
            cur.execute("DROP TRIGGER IF EXISTS trigger_kund_uppdatering ON Kunder;")
            cur.execute(
                """
                CREATE TRIGGER trigger_kund_uppdatering
                AFTER UPDATE ON Kunder
                FOR EACH ROW
                EXECUTE FUNCTION logga_kund_uppdatering();
                """
            )

            cur.execute(
                """
                CREATE OR REPLACE FUNCTION formatera_email()
                RETURNS TRIGGER AS $$
                BEGIN
                    NEW.Email = LOWER(NEW.Email);
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
            cur.execute("DROP TRIGGER IF EXISTS trigger_formatera_kund_email ON Kunder;")
            cur.execute(
                """
                CREATE TRIGGER trigger_formatera_kund_email
                BEFORE INSERT OR UPDATE ON Kunder
                FOR EACH ROW
                EXECUTE FUNCTION formatera_email();
                """
            )
            cur.execute("DROP TRIGGER IF EXISTS trigger_formatera_anstalld_email ON Anstallda;")
            cur.execute(
                """
                CREATE TRIGGER trigger_formatera_anstalld_email
                BEFORE INSERT OR UPDATE ON Anstallda
                FOR EACH ROW
                EXECUTE FUNCTION formatera_email();
                """
            )

            cur.execute(
                """
                CREATE OR REPLACE FUNCTION uppdatera_anstalld_timestamp()
                RETURNS TRIGGER AS $$
                BEGIN
                    NEW.UppdateradDatum = CURRENT_TIMESTAMP;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
            cur.execute("DROP TRIGGER IF EXISTS trigger_anstalld_timestamp ON Anstallda;")
            cur.execute(
                """
                CREATE TRIGGER trigger_anstalld_timestamp
                BEFORE UPDATE ON Anstallda
                FOR EACH ROW
                EXECUTE FUNCTION uppdatera_anstalld_timestamp();
                """
            )

            cur.execute("DROP VIEW IF EXISTS OrdersammanFattning CASCADE;")
            cur.execute(
                """
                CREATE OR REPLACE VIEW OrdersammanFattning AS
                SELECT
                    b.OrderID AS KvittoNummer,
                    b.Datum AS OrderDatum,
                    k.Namn AS KundNamn,
                    p.ProduktNamn AS ProduktNamn,
                    o.Antal AS Antal,
                    p.Pris AS Pris,
                    (o.Antal * p.Pris) AS RadSumma,
                    b.Totalbelopp AS TotalBelopp
                FROM Bestallningar b
                JOIN Kunder k ON b.KundID = k.KundID
                JOIN Orderrader o ON b.OrderID = o.OrderID
                JOIN Produkter p ON o.ProduktID = p.ProduktID;
                """
            )

            cur.execute(
                """
                CREATE OR REPLACE PROCEDURE BeraknaTotalForsaljning(
                    IN start_datum DATE,
                    IN slut_datum DATE
                )
                LANGUAGE plpgsql
                AS $$
                DECLARE
                    total NUMERIC(14, 2);
                BEGIN
                    SELECT COALESCE(SUM(Totalbelopp), 0)
                    INTO total
                    FROM Bestallningar
                    WHERE Datum::DATE BETWEEN start_datum AND slut_datum;

                    RAISE NOTICE 'Total forsaljning mellan % och % ar % kr', start_datum, slut_datum, total;
                END;
                $$;
                """
            )

            cur.execute(
                """
                INSERT INTO Produkttyp (ProduktTypID, ProduktTypNamn)
                VALUES
                    (1, 'Pizza'),
                    (2, 'Läsk & sås')
                ON CONFLICT (ProduktTypID) DO NOTHING;
                """
            )
            cur.execute(
                """
                UPDATE Produkttyp
                SET ProduktTypNamn = 'Läsk & sås'
                WHERE ProduktTypID = 2 AND ProduktTypNamn = 'Lask & Sas';
                """
            )
            cur.execute(
                """
                UPDATE Produkter SET ProduktNamn = 'Vitlokssås', Beskrivning = 'Krämig vitlokssås som passar till pizza, pommes och sallad.'
                WHERE ProduktNamn = 'Vitlokssas';
                """
            )
            cur.execute(
                """
                UPDATE Produkter SET ProduktNamn = 'Bearnaisesås', Beskrivning = 'Klassisk bea med tydlig dragon och krämig konsistens.'
                WHERE ProduktNamn = 'Bearnaisesas';
                """
            )

            for product in DEFAULT_PRODUCTS:
                cur.execute(
                    """
                    INSERT INTO Produkter (ProduktTypID, ProduktNamn, Pris, Beskrivning, LagerSaldo)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (ProduktNamn) DO NOTHING;
                    """,
                    (
                        product["type_id"],
                        product["name"],
                        product["price"],
                        product["description"],
                        product["stock"],
                    ),
                )
                cur.execute(
                    """
                    UPDATE Produkter
                    SET Beskrivning = %s
                    WHERE ProduktNamn = %s
                      AND COALESCE(Beskrivning, '') = '';
                    """,
                    (product["description"], product["name"]),
                )

            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM Anstallda
                WHERE Roll = %s AND Aktiv = TRUE;
                """,
                (STAFF_ROLE_ADMIN,),
            )
            if cur.fetchone()["n"] == 0:
                bootstrap_username = get_bootstrap_admin_username()
                bootstrap_email = f"{bootstrap_username.lower()}@alexbenjis.local"
                bootstrap_password = generate_password_hash(get_bootstrap_admin_password())

                cur.execute(
                    """
                    SELECT AnstalldID
                    FROM Anstallda
                    WHERE LOWER(Anvandarnamn) = LOWER(%s)
                       OR LOWER(Email) = LOWER(%s);
                    """,
                    (bootstrap_username, bootstrap_email),
                )
                existing_bootstrap = cur.fetchone()

                if existing_bootstrap:
                    cur.execute(
                        """
                        UPDATE Anstallda
                        SET Namn = %s,
                            Email = %s,
                            Roll = %s,
                            Losenord = %s,
                            Aktiv = TRUE
                        WHERE AnstalldID = %s;
                        """,
                        (
                            "Huvudadministrator",
                            bootstrap_email,
                            STAFF_ROLE_ADMIN,
                            bootstrap_password,
                            existing_bootstrap["anstalldid"],
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO Anstallda (
                            Anvandarnamn,
                            Namn,
                            Email,
                            Roll,
                            Losenord,
                            Aktiv
                        )
                        VALUES (%s, %s, %s, %s, %s, TRUE);
                        """,
                        (
                            bootstrap_username,
                            "Huvudadministrator",
                            bootstrap_email,
                            STAFF_ROLE_ADMIN,
                            bootstrap_password,
                        ),
                    )

            conn.commit()
            _SCHEMA_READY = True
        except Exception:
            safe_rollback(conn)
            raise
        finally:
            cur.close()
            conn.close()


@app.route("/")
def index():
    db_error = None
    meny = {}
    receipt = None
    customer_profile = None
    staff_profile = None
    conn = None
    cur = None

    try:
        conn = get_ready_connection()
        cur = conn.cursor()

        meny = fetch_menu(cur)
        customer_profile = get_logged_in_customer(cur)
        staff_profile = get_logged_in_staff(cur)

        if session.get("kund_id") and not customer_profile:
            clear_customer_session()
        if session.get("anstalld_id") and not staff_profile:
            clear_staff_session()

        last_id = request.args.get("last_id")
        if last_id:
            cur.execute(
                """
                SELECT *
                FROM OrdersammanFattning
                WHERE KvittoNummer = %s;
                """,
                (last_id,),
            )
            rows = cur.fetchall()
            if rows:
                receipt = {
                    "receiptno": rows[0]["kvittonummer"],
                    "orderdate": rows[0]["orderdatum"],
                    "customername": rows[0]["kundnamn"],
                    "finaltotal": rows[0]["totalbelopp"],
                    "itemlist": [
                        {
                            "name": row["produktnamn"],
                            "qty": row["antal"],
                            "price": float(row["pris"]),
                        }
                        for row in rows
                    ],
                }

    except Exception as exc:
        db_error = format_db_error(exc)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    return render_template(
        "index.html",
        meny=meny,
        receipt=receipt,
        customer_profile=customer_profile,
        staff_profile=staff_profile,
        error_login=request.args.get("error_login"),
        error_register=request.args.get("error_register"),
        error_profile=request.args.get("error_profile"),
        success_profile=request.args.get("success_profile"),
        db_error=db_error,
    )


@app.route("/order", methods=["POST"])
def place_order():
    kund_id = session.get("kund_id")
    if not kund_id:
        return redirect(url_for("index", error_login="Logga in för att beställa."))

    cart_data = request.form.get("cart_data")
    if not cart_data:
        return redirect(url_for("index"))

    try:
        cart = json.loads(cart_data)
    except Exception:
        return redirect(url_for("index"))

    bestallning_rader = [
        {"produkt_id": int(produkt_id), "antal": int(rad["qty"])}
        for produkt_id, rad in cart.items()
        if int(rad["qty"]) > 0
    ]
    if not bestallning_rader:
        return redirect(url_for("index"))

    leveransadress = normalize_text(request.form.get("leveransadress"))
    if not leveransadress:
        return redirect(
            url_for("index", error_profile="Ange en leveransadress innan du beställer.")
        )

    conn = None
    cur = None
    order_id = None

    try:
        conn = get_ready_connection()
        cur = conn.cursor()

        customer_profile = get_logged_in_customer(cur)
        if not customer_profile:
            clear_customer_session()
            return redirect(url_for("index", error_login="Logga in igen för att beställa."))

        totalbelopp = Decimal("0")
        for rad in bestallning_rader:
            cur.execute(
                "SELECT Pris, LagerSaldo FROM Produkter WHERE ProduktID = %s;",
                (rad["produkt_id"],),
            )
            produkt = cur.fetchone()
            if not produkt:
                return redirect(url_for("index", error_profile="En produkt saknas i databasen."))
            lagersaldo = produkt["lagersaldo"]
            antal = rad["antal"]
            if lagersaldo < antal:
                msg = format_stock_error(cur, rad["produkt_id"], antal, lagersaldo)
                return redirect(url_for("index", error_profile=msg))
            totalbelopp += produkt["pris"] * antal

        cur.execute("SELECT COALESCE(MAX(OrderID), 1000) + 1 AS next_id FROM Bestallningar;")
        order_id = cur.fetchone()["next_id"]

        cur.execute(
            """
            INSERT INTO Bestallningar (OrderID, KundID, Totalbelopp, Leveransadress)
            VALUES (%s, %s, %s, %s);
            """,
            (order_id, kund_id, totalbelopp, leveransadress),
        )

        for rad in bestallning_rader:
            cur.execute(
                """
                INSERT INTO Orderrader (OrderID, ProduktID, Antal)
                VALUES (%s, %s, %s);
                """,
                (order_id, rad["produkt_id"], rad["antal"]),
            )

        conn.commit()
    except Exception as exc:
        safe_rollback(conn)
        err_msg = str(exc)
        if "otillrackligt lager" in err_msg.lower() or "otillräckligt lager" in err_msg.lower():
            match = re.search(r"produkt\s+(\d+)", err_msg, re.IGNORECASE)
            if match and cur:
                produkt_id = int(match.group(1))
                cur.execute(
                    "SELECT ProduktNamn, LagerSaldo FROM Produkter WHERE ProduktID = %s;",
                    (produkt_id,),
                )
                row = cur.fetchone()
                if row:
                    lagersaldo = row["lagersaldo"]
                    err_msg = format_stock_error(cur, produkt_id, None, lagersaldo)
        return redirect(url_for("index", error_profile=err_msg))
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    return redirect(url_for("index", last_id=order_id))


@app.route("/register", methods=["POST"])
def register():
    namn = normalize_text(request.form.get("namn"))
    email = normalize_email(request.form.get("email"))
    telefon = normalize_text(request.form.get("telefon"))
    adress = normalize_text(request.form.get("adress"))
    losenord = request.form.get("losenord", "")

    if not namn or not email or not losenord:
        return redirect(url_for("index", error_register="Namn, email och lösenord måste fyllas i."))

    conn = None
    cur = None

    try:
        conn = get_ready_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO Kunder (Namn, Email, Telefon, Adress, Losenord)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING KundID, Namn;
            """,
            (namn, email, telefon or None, adress or None, generate_password_hash(losenord)),
        )
        row = cur.fetchone()
        conn.commit()
        set_customer_session(row)
        return redirect(url_for("index"))
    except psycopg2.errors.UniqueViolation:
        safe_rollback(conn)
        return redirect(url_for("index", error_register="Email redan registrerad."))
    except Exception as exc:
        safe_rollback(conn)
        return redirect(url_for("index", error_register=format_db_error(exc)))
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route("/login", methods=["POST"])
def login():
    email = normalize_email(request.form.get("email"))
    losenord = request.form.get("losenord", "")

    conn = None
    cur = None

    try:
        conn = get_ready_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT KundID, Namn, Losenord
            FROM Kunder
            WHERE Email = %s;
            """,
            (email,),
        )
        row = cur.fetchone()

        if row and check_password_hash(row["losenord"], losenord):
            set_customer_session(row)
            return redirect(url_for("index"))

        return redirect(url_for("index", error_login="Fel email eller lösenord."))
    except Exception as exc:
        return redirect(url_for("index", error_login=format_db_error(exc)))
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route("/profile/update", methods=["POST"])
def update_profile():
    kund_id = session.get("kund_id")
    if not kund_id:
        return redirect(url_for("index", error_login="Logga in för att uppdatera din profil."))

    namn = normalize_text(request.form.get("namn"))
    email = normalize_email(request.form.get("email"))
    telefon = normalize_text(request.form.get("telefon"))
    adress = normalize_text(request.form.get("adress"))
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not namn or not email:
        return redirect(url_for("index", error_profile="Namn och email måste fyllas i."))

    if new_password and new_password != confirm_password:
        return redirect(url_for("index", error_profile="De nya lösenorden matchar inte."))

    conn = None
    cur = None

    try:
        conn = get_ready_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT KundID, Losenord
            FROM Kunder
            WHERE KundID = %s;
            """,
            (kund_id,),
        )
        customer = cur.fetchone()

        if not customer:
            clear_customer_session()
            return redirect(url_for("index", error_login="Kunde inte hitta ditt konto."))

        password_hash = customer["losenord"]
        if new_password:
            if not current_password:
                return redirect(
                    url_for(
                        "index",
                        error_profile="Ange ditt nuvarande lösenord för att byta lösenord.",
                    )
                )
            if not check_password_hash(password_hash, current_password):
                return redirect(url_for("index", error_profile="Nuvarande lösenord är fel."))
            password_hash = generate_password_hash(new_password)

        cur.execute(
            """
            UPDATE Kunder
            SET Namn = %s,
                Email = %s,
                Telefon = %s,
                Adress = %s,
                Losenord = %s
            WHERE KundID = %s;
            """,
            (namn, email, telefon or None, adress or None, password_hash, kund_id),
        )
        conn.commit()

        session["kund_namn"] = namn
        return redirect(url_for("index", success_profile="Profilen uppdaterades."))
    except psycopg2.errors.UniqueViolation:
        safe_rollback(conn)
        return redirect(url_for("index", error_profile="Email används redan av en annan användare."))
    except Exception as exc:
        safe_rollback(conn)
        return redirect(url_for("index", error_profile=format_db_error(exc)))
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route("/logout")
def logout():
    clear_customer_session()
    return redirect(url_for("index"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        identifier = normalize_text(request.form.get("identifier"))
        password = request.form.get("password", "")

        if not identifier or not password:
            return redirect(url_for("admin_login", error="Användarnamn och lösenord måste fyllas i."))

        conn = None
        cur = None

        try:
            conn = get_ready_connection()
            cur = conn.cursor()
            staff = fetch_staff_by_identifier(cur, identifier)

            if not staff or not check_password_hash(staff["losenord"], password):
                return redirect(url_for("admin_login", error="Fel användarnamn eller lösenord."))
            if not staff["aktiv"]:
                return redirect(url_for("admin_login", error="Detta konto är inaktiverat."))
            if staff["roll"] != STAFF_ROLE_ADMIN:
                return redirect(url_for("admin_login", error="Detta konto saknar admin-behorighet."))

            set_staff_session(staff)
            return redirect(url_for("admin_dashboard"))
        except Exception as exc:
            return redirect(url_for("admin_login", error=format_db_error(exc)))
        finally:
            if cur:
                cur.close()
            if conn:
                conn.close()

    db_error = None
    try:
        ensure_database_schema()
    except Exception as exc:
        db_error = format_db_error(exc)

    return render_template(
        "admin.html",
        mode="login",
        error=request.args.get("error"),
        db_error=db_error,
        default_admin_username=get_bootstrap_admin_username(),
    )


@app.route("/admin/logout")
def admin_logout():
    clear_staff_session()
    return redirect(url_for("admin_login"))


@app.route("/admin", methods=["GET", "POST"])
def admin_dashboard():
    if not staff_has_role(STAFF_ROLE_ADMIN):
        return redirect(url_for("admin_login"))

    db_error = None
    data = {
        "counts": {},
        "kunder": [],
        "anstallda": [],
        "produkter": [],
        "produkttyper": [],
        "bestallningar": [],
        "orderrader": [],
        "kundlogg": [],
        "ordersammanfattning": [],
        "sales": None,
        "db_name": DB_CONFIG["dbname"],
    }
    current_staff = None
    conn = None
    cur = None

    start_datum = request.form.get("start_datum", "")
    slut_datum = request.form.get("slut_datum", "")

    try:
        conn = get_ready_connection()
        cur = conn.cursor()

        current_staff = get_logged_in_staff(cur)
        if not current_staff or current_staff["roll"] != STAFF_ROLE_ADMIN:
            clear_staff_session()
            return redirect(url_for("admin_login", error="Logga in igen som admin."))

        data = fetch_admin_dashboard_data(cur, start_datum=start_datum, slut_datum=slut_datum)
    except Exception as exc:
        db_error = format_db_error(exc)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    return render_template(
        "admin.html",
        mode="dashboard",
        db_error=db_error,
        error=request.args.get("error"),
        success=request.args.get("success"),
        data=data,
        current_staff=current_staff,
        role_label=role_label,
    )


@app.route("/admin/staff/create", methods=["POST"])
def admin_create_staff():
    if not staff_has_role(STAFF_ROLE_ADMIN):
        return redirect(url_for("admin_login"))

    anvandarnamn = normalize_text(request.form.get("anvandarnamn"))
    namn = normalize_text(request.form.get("namn"))
    email = normalize_email(request.form.get("email"))
    telefon = normalize_text(request.form.get("telefon"))
    roll = normalize_text(request.form.get("roll")) or STAFF_ROLE_EMPLOYEE
    losenord = request.form.get("losenord", "")

    if roll not in {STAFF_ROLE_ADMIN, STAFF_ROLE_EMPLOYEE}:
        roll = STAFF_ROLE_EMPLOYEE

    if not anvandarnamn or not namn or not email or not losenord:
        return redirect(url_for("admin_dashboard", error="Fyll i användarnamn, namn, email och lösenord."))

    conn = None
    cur = None

    try:
        conn = get_ready_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO Anstallda (
                Anvandarnamn,
                Namn,
                Email,
                Telefon,
                Roll,
                Losenord,
                SkapadAv
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s);
            """,
            (
                anvandarnamn,
                namn,
                email,
                telefon or None,
                roll,
                generate_password_hash(losenord),
                session.get("anstalld_id"),
            ),
        )
        conn.commit()
        return redirect(url_for("admin_dashboard", success="Det nya kontot skapades."))
    except psycopg2.errors.UniqueViolation:
        safe_rollback(conn)
        return redirect(url_for("admin_dashboard", error="Användarnamn eller email finns redan."))
    except Exception as exc:
        safe_rollback(conn)
        return redirect(url_for("admin_dashboard", error=format_db_error(exc)))
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route("/admin/staff/<int:anstalld_id>/update", methods=["POST"])
def admin_update_staff(anstalld_id):
    if not staff_has_role(STAFF_ROLE_ADMIN):
        return redirect(url_for("admin_login"))

    anvandarnamn = normalize_text(request.form.get("anvandarnamn"))
    namn = normalize_text(request.form.get("namn"))
    email = normalize_email(request.form.get("email"))
    telefon = normalize_text(request.form.get("telefon"))
    roll = normalize_text(request.form.get("roll")) or STAFF_ROLE_EMPLOYEE
    nytt_losenord = request.form.get("nytt_losenord", "")
    aktiv = normalize_checkbox(request.form.get("aktiv"))

    if roll not in {STAFF_ROLE_ADMIN, STAFF_ROLE_EMPLOYEE}:
        roll = STAFF_ROLE_EMPLOYEE

    if not anvandarnamn or not namn or not email:
        return redirect(url_for("admin_dashboard", error="Namn, email och användarnamn måste fyllas i."))

    conn = None
    cur = None

    try:
        conn = get_ready_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT AnstalldID, Roll, Aktiv, Losenord
            FROM Anstallda
            WHERE AnstalldID = %s;
            """,
            (anstalld_id,),
        )
        target = cur.fetchone()

        if not target:
            return redirect(url_for("admin_dashboard", error="Kunde inte hitta det valda kontot."))

        cur.execute(
            """
            SELECT COUNT(*) AS n
            FROM Anstallda
            WHERE Roll = %s AND Aktiv = TRUE;
            """,
            (STAFF_ROLE_ADMIN,),
        )
        active_admins = cur.fetchone()["n"]

        if anstalld_id == session.get("anstalld_id") and not aktiv:
            return redirect(url_for("admin_dashboard", error="Du kan inte inaktivera ditt eget konto."))

        if target["roll"] == STAFF_ROLE_ADMIN and active_admins <= 1:
            if roll != STAFF_ROLE_ADMIN or not aktiv:
                return redirect(url_for("admin_dashboard", error="Det måste finnas minst en aktiv admin."))

        password_hash = target["losenord"]
        if nytt_losenord:
            password_hash = generate_password_hash(nytt_losenord)

        cur.execute(
            """
            UPDATE Anstallda
            SET Anvandarnamn = %s,
                Namn = %s,
                Email = %s,
                Telefon = %s,
                Roll = %s,
                Aktiv = %s,
                Losenord = %s
            WHERE AnstalldID = %s
            RETURNING
                AnstalldID,
                Anvandarnamn,
                Namn,
                Email,
                Telefon,
                Roll,
                Aktiv,
                SkapadAv,
                SkapadDatum,
                UppdateradDatum;
            """,
            (
                anvandarnamn,
                namn,
                email,
                telefon or None,
                roll,
                aktiv,
                password_hash,
                anstalld_id,
            ),
        )
        updated_row = cur.fetchone()
        conn.commit()

        if anstalld_id == session.get("anstalld_id"):
            if updated_row["aktiv"]:
                set_staff_session(updated_row)
            else:
                clear_staff_session()

            if updated_row["roll"] != STAFF_ROLE_ADMIN:
                return redirect(url_for("employee_dashboard", success="Ditt konto uppdaterades."))

        return redirect(url_for("admin_dashboard", success="Kontot uppdaterades."))
    except psycopg2.errors.UniqueViolation:
        safe_rollback(conn)
        return redirect(url_for("admin_dashboard", error="Användarnamn eller email finns redan."))
    except Exception as exc:
        safe_rollback(conn)
        return redirect(url_for("admin_dashboard", error=format_db_error(exc)))
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route("/admin/product/<int:produkt_id>/update", methods=["POST"])
def admin_update_product(produkt_id):
    if not staff_has_role(STAFF_ROLE_ADMIN):
        return redirect(url_for("admin_login"))

    produktnamn = normalize_text(request.form.get("produktnamn"))
    beskrivning = normalize_text(request.form.get("beskrivning"))

    if not produktnamn:
        return redirect(url_for("admin_dashboard", error="Produktnamn får inte vara tomt."))

    try:
        pris = parse_price(request.form.get("pris"))
        lagersaldo = parse_non_negative_int(request.form.get("lagersaldo"), "Lagersaldo")
        produkttyp_id = parse_non_negative_int(request.form.get("produkttypid"), "ProduktTypID")
    except ValueError as exc:
        return redirect(url_for("admin_dashboard", error=str(exc)))

    conn = None
    cur = None

    try:
        conn = get_ready_connection()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT 1
            FROM Produkttyp
            WHERE ProduktTypID = %s;
            """,
            (produkttyp_id,),
        )
        if not cur.fetchone():
            return redirect(url_for("admin_dashboard", error="Vald produkttyp finns inte."))

        cur.execute(
            """
            UPDATE Produkter
            SET ProduktNamn = %s,
                Pris = %s,
                LagerSaldo = %s,
                ProduktTypID = %s,
                Beskrivning = %s
            WHERE ProduktID = %s;
            """,
            (produktnamn, pris, lagersaldo, produkttyp_id, beskrivning, produkt_id),
        )
        conn.commit()
        return redirect(url_for("admin_dashboard", success="Produkten uppdaterades."))
    except psycopg2.errors.UniqueViolation:
        safe_rollback(conn)
        return redirect(url_for("admin_dashboard", error="Produktnamnet finns redan."))
    except Exception as exc:
        safe_rollback(conn)
        return redirect(url_for("admin_dashboard", error=format_db_error(exc)))
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route("/anstallda")
def employee_dashboard():
    db_error = None
    current_staff = None
    products = []
    conn = None
    cur = None

    try:
        conn = get_ready_connection()
        cur = conn.cursor()
        current_staff = get_logged_in_staff(cur)

        if current_staff:
            products = fetch_staff_products(cur)
        elif session.get("anstalld_id"):
            clear_staff_session()
    except Exception as exc:
        db_error = format_db_error(exc)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    if not current_staff:
        return render_template(
            "anstallda.html",
            mode="login",
            error=request.args.get("error"),
            success=request.args.get("success"),
            db_error=db_error,
        )

    return render_template(
        "anstallda.html",
        mode="dashboard",
        error=request.args.get("error"),
        success=request.args.get("success"),
        db_error=db_error,
        current_staff=current_staff,
        products=products,
        role_label=role_label,
    )


@app.route("/anstallda/login", methods=["POST"])
def employee_login():
    identifier = normalize_text(request.form.get("identifier"))
    password = request.form.get("password", "")

    if not identifier or not password:
        return redirect(url_for("employee_dashboard", error="Användarnamn och lösenord måste fyllas i."))

    conn = None
    cur = None

    try:
        conn = get_ready_connection()
        cur = conn.cursor()
        staff = fetch_staff_by_identifier(cur, identifier)

        if not staff or not check_password_hash(staff["losenord"], password):
            return redirect(url_for("employee_dashboard", error="Fel användarnamn eller lösenord."))
        if not staff["aktiv"]:
            return redirect(url_for("employee_dashboard", error="Detta konto är inaktiverat."))

        set_staff_session(staff)
        if staff["roll"] == STAFF_ROLE_ADMIN:
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("employee_dashboard"))
    except Exception as exc:
        return redirect(url_for("employee_dashboard", error=format_db_error(exc)))
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route("/anstallda/logout")
def employee_logout():
    clear_staff_session()
    return redirect(url_for("employee_dashboard"))


@app.route("/anstallda/product/<int:produkt_id>/description", methods=["POST"])
def employee_update_product_description(produkt_id):
    if not staff_has_role(STAFF_ROLE_ADMIN, STAFF_ROLE_EMPLOYEE):
        return redirect(url_for("employee_dashboard", error="Logga in som personal för att redigera beskrivningar."))

    beskrivning = normalize_text(request.form.get("beskrivning"))

    conn = None
    cur = None

    try:
        conn = get_ready_connection()
        cur = conn.cursor()

        current_staff = get_logged_in_staff(cur)
        if not current_staff:
            clear_staff_session()
            return redirect(url_for("employee_dashboard", error="Logga in igen för att fortsätta."))

        cur.execute(
            """
            SELECT p.ProduktID
            FROM Produkter p
            JOIN Produkttyp pt ON p.ProduktTypID = pt.ProduktTypID
            WHERE p.ProduktID = %s
              AND pt.ProduktTypID IN (1, 2);
            """,
            (produkt_id,),
        )
        if not cur.fetchone():
            return redirect(url_for("employee_dashboard", error="Endast pizzor och tillbehör kan uppdateras från personalvyn."))

        cur.execute(
            """
            UPDATE Produkter
            SET Beskrivning = %s
            WHERE ProduktID = %s;
            """,
            (beskrivning, produkt_id),
        )
        conn.commit()
        return redirect(url_for("employee_dashboard", success="Beskrivningen uppdaterades."))
    except Exception as exc:
        safe_rollback(conn)
        return redirect(url_for("employee_dashboard", error=format_db_error(exc)))
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False, port=5001)
