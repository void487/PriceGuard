#!/usr/bin/env python3
"""
PriceGuard – monitor numbers from the web (GUI + batch)

Main features
- Select a number via Shift+Click (works inside Shadow DOM). The CSS selector and baseline value are stored.
- Universal loading without per-site tuning:
    • the headless context mimics a real browser (UA, locale cs-CZ, timezone Europe/Prague),
    • polling happens within the DOM (no endless reloads),
    • text reading fallbacks (inner_text → text_content → JS innerText → inner_html),
    • smart candidate scan (meta/itemprop/JSON-LD/"price" classes/attributes) with nearest-to-baseline selection.
- Table columns: URL, Description (editable), Baseline, Last, Fetched, Change, Bonus I, Bonus II, Active, Actions (internal ID is hidden).
- Dark/Light theme with a toggle (Automatic by default) and subtle row highlighting (Actions column is never colored).
- "Check"/"Check active" buttons show progress (⏳) and are temporarily disabled.
- Batch mode (`--batch`) sends an email (drops + errors). The GUI never sends emails.

Dependencies:
  pip install PySide6 qasync playwright==1.47.0 python-dotenv
  playwright install

Run:
  python priceguard.py          # GUI
  python priceguard.py --batch  # batch mode, returns 1 if a drop or error occurs, otherwise 0
"""
from __future__ import annotations
import asyncio
import inspect
import math
import random
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Callable, Awaitable, Union, Any

from dotenv import load_dotenv
from PySide6 import QtCore, QtWidgets, QtGui
from PySide6.QtCharts import (
    QChart,
    QChartView,
    QDateTimeAxis,
    QLineSeries,
    QValueAxis
)
from PySide6.QtCore import Qt, QSettings
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QMessageBox, QAbstractItemView, QHeaderView, QCheckBox, QLabel,
    QDialogButtonBox
)
from qasync import QEventLoop
from playwright.async_api import async_playwright

APP_NAME = "PriceGuard"
ORG_NAME = "PriceGuard"
DB_PATH = "targets.db"
TIMEOUT_MS = 60000  # ms

CREATE_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS targets(
  id INTEGER PRIMARY KEY,
  url TEXT NOT NULL,
  selector TEXT NOT NULL,
  attr TEXT NOT NULL DEFAULT 'textContent',
  baseline REAL NOT NULL,
  active INT NOT NULL DEFAULT 1,
  note TEXT,
  created_at DATETIME NOT NULL,
  description TEXT,
  timeout_ms INT,
  bonus1_selector TEXT,
  bonus1_text TEXT,
  bonus2_selector TEXT,
  bonus2_text TEXT
);
CREATE TABLE IF NOT EXISTS checks(
  id INTEGER PRIMARY KEY,
  target_id INT NOT NULL,
  observed REAL NOT NULL,
  ok INT NOT NULL,
  fetched_at DATETIME NOT NULL,
  details TEXT
);
CREATE TABLE IF NOT EXISTS daily_stats(
  id INTEGER PRIMARY KEY,
  target_id INT NOT NULL,
  stat_date TEXT NOT NULL,
  observed REAL,
  bonus1_present INT,
  bonus2_present INT,
  recorded_at DATETIME NOT NULL,
  UNIQUE(target_id, stat_date)
);
"""

CSS_PATH_HELPER = r"""
window.__cssPath = function(el) {
  if (!(el instanceof Element)) return null;
  if (el.id) return '#' + CSS.escape(el.id);
  const parts = [];
  while (el && el.nodeType === Node.ELEMENT_NODE && parts.length < 12) {
    let selector = el.nodeName.toLowerCase();
    if (el.classList.length) {
      selector += '.' + CSS.escape(el.classList[0]);
    }
    let sib = el, nth = 1;
    while (sib = sib.previousElementSibling) {
      if (sib.nodeName === el.nodeName) nth++;
    }
    selector += `:nth-of-type(${nth})`;
    parts.unshift(selector);
    el = el.parentElement;
    if (el && el.id) { parts.unshift('#' + CSS.escape(el.id)); break; }
  }
  return parts.join(' > ');
};
"""

HILITE_STYLE = r"""
(() => {
  const s = document.createElement('style');
  s.textContent = `@keyframes __flash{0%{outline:3px solid rgba(0,170,255,1)}100%{outline:3px solid rgba(0,170,255,0)}}`;
  document.head.appendChild(s);
})();
"""

PICKER_JS = r"""
(() => {
  const pickElementFromEvent = (e) => {
    const path = e.composedPath ? e.composedPath() : (e.path || []);
    for (const n of path) if (n && n.nodeType === 1) return n;
    return e.target instanceof Element ? e.target : null;
  };
  const flash = (el) => {
    const prev = el.style.outline;
    el.style.outline = '3px solid #00AAFF';
    setTimeout(() => { el.style.outline = prev; }, 400);
  };
  const onClick = (e) => {
    if (!e.shiftKey) return;
    const el = pickElementFromEvent(e);
    if (!el) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    const sel = window.__cssPath ? window.__cssPath(el) : null;
    const text = (el.innerText || el.textContent || '').trim();
    try { flash(el); } catch(_) {}
    if (window.__picked) window.__picked(sel, text);
    document.removeEventListener('click', onClick, true);
    window.removeEventListener('click', onClick, true);
  };
  document.addEventListener('click', onClick, true);
  window.addEventListener('click', onClick, true);
  console.log('[PriceGuard] Shift+Click a number to select it (Shadow DOM supported).');
})();
"""

# ---------- DB & model ----------
def init_db() -> None:
    need_create = not Path(DB_PATH).exists()
    with sqlite3.connect(DB_PATH) as con:
        if need_create:
            con.executescript(CREATE_SQL)
        # Migrace: description, timeout_ms, bonus columns
        try:
            con.execute("ALTER TABLE targets ADD COLUMN description TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            con.execute("ALTER TABLE targets ADD COLUMN timeout_ms INT")
        except sqlite3.OperationalError:
            pass
        for col in ("bonus1_selector TEXT", "bonus1_text TEXT", "bonus2_selector TEXT", "bonus2_text TEXT"):
            try:
                con.execute(f"ALTER TABLE targets ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        # Daily measurement history
        con.execute(
            "CREATE TABLE IF NOT EXISTS daily_stats("
            "id INTEGER PRIMARY KEY,"
            "target_id INT NOT NULL,"
            "stat_date TEXT NOT NULL,"
            "observed REAL,"
            "bonus1_present INT,"
            "bonus2_present INT,"
            "recorded_at DATETIME NOT NULL,"
            "UNIQUE(target_id, stat_date)"
            ")"
        )
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_stats_target_date "
            "ON daily_stats(target_id, stat_date)"
        )

def parse_number(text: str) -> float:
    """Extract the first meaningful number from text (cz/en formats) and convert it to float."""
    t = str(text)
    t = (t.replace("\u00A0", " ").replace("\u202F", " ").replace("\u2007", " ").strip())
    m = re.search(r"-?\d[\d.,\s]*\d", t)
    if not m:
        raise ValueError(f"Unable to find a number in: {text!r}")
    num = m.group(0)
    num = re.sub(r"[^\d.,\s-]", "", num).replace(" ", "")
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        num = num.replace(",", ".")
    num = num.strip(".,")
    if not num or num in {"-", "."}:
        raise ValueError(f"Unable to parse a number from: {text!r}")
    return float(num)

@dataclass
class Target:
    id: int
    url: str
    selector: str
    attr: str
    baseline: float
    active: int
    note: Optional[str]
    created_at: str
    description: Optional[str] = None
    timeout_ms: Optional[int] = None
    bonus1_selector: Optional[str] = None
    bonus1_text: Optional[str] = None
    bonus2_selector: Optional[str] = None
    bonus2_text: Optional[str] = None

# ---- Playwright helpers ----
class HeadlessBrowserManager:
    """Manage a shared headless browser across measurements."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def ensure_running(self) -> None:
        if self._browser is not None:
            return
        async with self._lock:
            if self._browser is not None:
                return
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )

    async def new_page(self, timeout_ms: Optional[int] = None):
        await self.ensure_running()
        context = await self._browser.new_context(
            ignore_https_errors=True,
            locale="cs-CZ",
            timezone_id="Europe/Prague",
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 840},
            extra_http_headers={"Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8"}
        )
        page = await context.new_page()
        timeout = timeout_ms if timeout_ms is not None else TIMEOUT_MS
        page.set_default_timeout(timeout)
        page.set_default_navigation_timeout(timeout)
        return context, page

    async def close(self) -> None:
        async with self._lock:
            browser, pw = self._browser, self._pw
            self._browser = None
            self._pw = None
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass

    async def restart(self) -> None:
        await self.close()
        await self.ensure_running()

    async def __aenter__(self):
        await self.ensure_running()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()


BrowserFactory = Union[
    HeadlessBrowserManager,
    Callable[[], Union[Awaitable[Tuple[Any, Any]], Tuple[Any, Any]]]
]


async def launch_browser_headed():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    ctx = await browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 840})
    page = await ctx.new_page()
    return pw, browser, page

async def launch_browser_headless(manager: Optional[HeadlessBrowserManager] = None, timeout_ms: Optional[int] = None):
    owns_manager = False
    if manager is None:
        manager = HeadlessBrowserManager()
        await manager.ensure_running()
        owns_manager = True
    context, page = await manager.new_page(timeout_ms=timeout_ms)
    return manager, context, page, owns_manager

async def capture_target(url: str) -> Tuple[str, float]:
    pw, browser, page = await launch_browser_headed()
    try:
        fut = asyncio.get_event_loop().create_future()

        def on_close():
            if not fut.done():
                fut.set_exception(RuntimeError("Selection was cancelled (window closed)."))

        page.on("close", on_close)
        browser.on("disconnected", on_close)

        async def on_pick(selector, text_value):
            if not fut.done():
                fut.set_result((selector, text_value))
            return "ok"
        await page.expose_function("__picked", on_pick)

        await page.add_init_script(CSS_PATH_HELPER)
        await page.add_init_script(HILITE_STYLE)
        await page.add_init_script(PICKER_JS)

        await page.goto(url, wait_until="domcontentloaded")
        await page.evaluate(PICKER_JS)

        selector, _ = await fut
        await page.wait_for_selector(selector, timeout=10000)
        txt = await page.locator(selector).inner_text()
        value = parse_number(txt)
        return selector, value
    finally:
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await pw.stop()
        except Exception:
            pass

async def capture_text_snippet(url: str) -> Tuple[str, str]:
    pw, browser, page = await launch_browser_headed()
    try:
        fut = asyncio.get_event_loop().create_future()

        def on_close():
            if not fut.done():
                fut.set_exception(RuntimeError("Selection was cancelled (window closed)."))

        page.on("close", on_close)
        browser.on("disconnected", on_close)

        async def on_pick(selector, text_value):
            if not fut.done():
                fut.set_result((selector, (text_value or "").strip()))
            return "ok"

        await page.expose_function("__picked", on_pick)
        await page.add_init_script(CSS_PATH_HELPER)
        await page.add_init_script(HILITE_STYLE)
        await page.add_init_script(PICKER_JS)

        await page.goto(url, wait_until="domcontentloaded")
        await page.evaluate(PICKER_JS)

        selector, text_value = await fut
        return selector, text_value
    finally:
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await pw.stop()
        except Exception:
            pass

async def _maybe_accept_cookies(page):
    sel = "#onetrust-accept-btn-handler, button#onetrust-accept-btn-handler, button:has-text('P\\u0159ijmout v\\u0161e'), button:has-text('P\\u0159ijmout'), button:has-text('Souhlas\\u00edm')"
    try:
        loc = page.locator(sel)
        if await loc.count():
            await loc.first.click(timeout=2000)
    except Exception:
        pass

async def fetch_target_data(
    t: Target,
    timeout_ms: int,
    browser_factory: Optional[BrowserFactory] = None
) -> Tuple[float, Dict[int, Optional[str]]]:
    """Single navigation + DOM polling + early candidate scan and loading of bonus texts."""
    import time as _time

    context = None
    page = None
    manager: Optional[HeadlessBrowserManager] = None
    owns_manager = False

    try:
        if browser_factory is None or isinstance(browser_factory, HeadlessBrowserManager):
            manager_input = browser_factory if isinstance(browser_factory, HeadlessBrowserManager) else None
            manager, context, page, owns_manager = await launch_browser_headless(manager_input, timeout_ms=timeout_ms)
        elif callable(browser_factory):
            result = browser_factory()
            if inspect.isawaitable(result):
                context, page = await result
            else:
                context, page = result
        else:
            raise TypeError("browser_factory must be a HeadlessBrowserManager or a callable returning (context, page)")

        if context is None or page is None:
            raise RuntimeError("browser_factory must return (context, page)")

        timeout = timeout_ms if timeout_ms is not None else TIMEOUT_MS
        try:
            page.set_default_timeout(timeout)
            page.set_default_navigation_timeout(timeout)
        except Exception:
            pass

        await page.goto(t.url, wait_until="domcontentloaded", timeout=timeout_ms)
        await _maybe_accept_cookies(page)

        loc = page.locator(t.selector)
        await loc.wait_for(state="attached", timeout=timeout_ms)

        start = _time.monotonic()
        did_candidate_scan = False
        last_text_sample = ""
        price_value: Optional[float] = None

        while (_time.monotonic() - start) * 1000 < timeout_ms:
            # Wait until digits appear inside the element
            try:
                handle = await loc.element_handle()
                if handle is not None:
                    await page.wait_for_function(
                        "(el) => /\\d/.test((el.innerText||el.textContent||'').trim())",
                        arg=handle,
                        timeout=1200
                    )
            except Exception:
                pass

            # Read text (fallback chain)
            txt = ""
            try:
                txt = (await loc.inner_text()).strip()
            except Exception:
                pass
            if not txt:
                try:
                    raw = await loc.text_content()
                    txt = (raw or "").strip()
                except Exception:
                    pass
            if not txt:
                try:
                    txt = (await loc.evaluate("el => (el.innerText || el.textContent || '').trim()")) or ""
                except Exception:
                    pass
            if not txt:
                try:
                    html = await loc.inner_html()
                    txt = html or ""
                except Exception:
                    pass

            if txt:
                last_text_sample = txt
                try:
                    price_value = parse_number(txt)
                    break
                except Exception:
                    pass  # still try the candidate scan below

            # One-time candidate scan after ~1.2 s
            if price_value is None and not did_candidate_scan and (_time.monotonic() - start) > 1.2:
                did_candidate_scan = True
                try:
                    cands = await page.evaluate("""() => {
                      const out = [];
                      const pushEl = (el, note) => {
                        let txt = '';
                        if (el.tagName && el.tagName.toLowerCase() === 'meta') {
                          txt = el.getAttribute('content') || '';
                        } else {
                          txt = (el.innerText || el.textContent || '').trim();
                        }
                        if (txt && /\\d/.test(txt)) out.push({txt, note});
                      };
                      const sels = [
                        'meta[itemprop="price"]',
                        'meta[property="product:price:amount"]',
                        '[itemprop="price"]',
                        '[data-price]',
                        '[data-amount]',
                        '[data-price-amount]',
                        '[class*="price"]',
                        '[id*="price"]'
                      ];
                      for (const s of sels) document.querySelectorAll(s).forEach(el => pushEl(el, s));
                      // JSON-LD
                      document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
                        try {
                          const j = JSON.parse(s.textContent); const arr = Array.isArray(j) ? j : [j];
                          const walk = (o) => {
                            if (!o || typeof o !== 'object') return;
                            if (o.price) out.push({txt: String(o.price), note: 'jsonld.price'});
                            if (o.offers) {
                              if (Array.isArray(o.offers)) o.offers.forEach(walk); else walk(o.offers);
                            }
                            for (const k in o) if (typeof o[k] === 'object') walk(o[k]);
                          };
                          arr.forEach(walk);
                        } catch(e) {}
                      });
                      return out.slice(0, 300);
                    }""")
                    vals = []
                    for c in cands or []:
                        txtc = c.get('txt') or ''
                        try:
                            v = parse_number(txtc)
                            if v > 0:
                                vals.append(v)
                        except Exception:
                            continue
                    if vals:
                        baseline = t.baseline
                        if baseline is not None:
                            vals.sort(key=lambda x: abs(x - float(baseline)))
                        else:
                            vals.sort(key=lambda x: -x)
                        price_value = float(vals[0])
                        break
                except Exception:
                    pass

            await page.wait_for_timeout(250)

        if price_value is None:
            raise RuntimeError(f"Timed out without finding a number (sample: {last_text_sample[:80]!r})")

        async def _read_bonus(sel: str) -> Optional[str]:
            try:
                loc_bonus = page.locator(sel)
                await loc_bonus.wait_for(state="attached", timeout=min(2000, timeout_ms))
                txt_bonus = ""
                try:
                    txt_bonus = (await loc_bonus.inner_text()).strip()
                except Exception:
                    pass
                if not txt_bonus:
                    try:
                        raw = await loc_bonus.text_content()
                        txt_bonus = (raw or "").strip()
                    except Exception:
                        pass
                if not txt_bonus:
                    try:
                        txt_bonus = (await loc_bonus.evaluate("el => (el.innerText || el.textContent || '').trim()")) or ""
                    except Exception:
                        pass
                return txt_bonus or None
            except Exception:
                return None

        bonus: Dict[int, Optional[str]] = {}
        for idx, sel in ((1, t.bonus1_selector), (2, t.bonus2_selector)):
            if sel and sel.strip():
                bonus[idx] = await _read_bonus(sel.strip())

        return float(price_value), bonus
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if owns_manager and manager is not None:
            try:
                await manager.close()
            except Exception:
                pass

# ---- Email ----
def send_email(subject: str, html_body: str) -> None:
    load_dotenv(override=False)
    host = os.getenv("SMTP_HOST")
    port_raw = os.getenv("SMTP_PORT", "587")
    try:
        port = int(port_raw)
    except ValueError as exc:
        print(f"[EMAIL] Invalid SMTP port '{port_raw}': {exc}")
        return
    user = os.getenv("SMTP_USER")
    pwd = os.getenv("SMTP_PASS")
    from_addr = os.getenv("SMTP_FROM", user or "")
    to_raw = os.getenv("SMTP_TO")
    if not (host and user and pwd and to_raw):
        print("[EMAIL] SMTP is not configured – skipped.")
        return
    to_list = [x.strip() for x in to_raw.split(",") if x.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_list)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    import smtplib
    try:
        with smtplib.SMTP(host, port) as s:
            s.starttls()
            s.login(user, pwd)
            s.sendmail(from_addr, to_list, msg.as_string())
    except (smtplib.SMTPException, OSError) as exc:
        print(f"[EMAIL] Failed to send email: {exc}")
        return
    print(f"[EMAIL] Sent to {to_list}")

# ---- DB helpers ----
def db_all_targets() -> List[Target]:
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM targets ORDER BY id DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.setdefault("description", None)
            d.setdefault("timeout_ms", None)
            d.setdefault("bonus1_selector", None)
            d.setdefault("bonus1_text", None)
            d.setdefault("bonus2_selector", None)
            d.setdefault("bonus2_text", None)
            d.pop("wait_selector", None)
            out.append(Target(**d))
        return out

def db_insert_target(url: str, selector: str, baseline: float, description: Optional[str]) -> int:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "INSERT INTO targets(url,selector,attr,baseline,active,note,created_at,description) VALUES (?,?,?,?,?,?,?,?)",
            (url, selector, 'textContent', baseline, 1, None, datetime.now(timezone.utc).isoformat(), description)
        )
        return cur.lastrowid

def db_update_description(target_id: int, description: str) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE targets SET description=? WHERE id=?", (description, target_id))

def db_update_bonus(target_id: int, index: int, selector: Optional[str], text_value: Optional[str]) -> None:
    sel_col = "bonus1_selector" if index == 1 else "bonus2_selector"
    text_col = "bonus1_text" if index == 1 else "bonus2_text"
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            f"UPDATE targets SET {sel_col}=?, {text_col}=? WHERE id=?",
            (selector, text_value, target_id)
        )

def db_update_bonus_text(target_id: int, index: int, text_value: Optional[str]) -> None:
    text_col = "bonus1_text" if index == 1 else "bonus2_text"
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            f"UPDATE targets SET {text_col}=? WHERE id=?",
            (text_value, target_id)
        )

def db_delete_target(target_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM checks WHERE target_id=?", (target_id,))
        con.execute("DELETE FROM targets WHERE id=?", (target_id,))

def db_set_active(target_id: int, active: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE targets SET active=? WHERE id=?", (active, target_id))

def db_insert_check(target_id: int, observed: float, ok: int, details: Optional[str]) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO checks(target_id,observed,ok,fetched_at,details) VALUES (?,?,?,?,?)",
            (target_id, observed, ok, datetime.now(timezone.utc).isoformat(), details)
        )

def db_log_daily_stat(target_id: int, observed: Optional[float], bonus_presence: Dict[int, Optional[bool]]) -> None:
    today = datetime.now(timezone.utc).astimezone().date().isoformat()
    recorded_at = datetime.now(timezone.utc).isoformat()

    def _to_int(flag: Optional[bool]) -> Optional[int]:
        if flag is None:
            return None
        return 1 if flag else 0

    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        exists = con.execute(
            "SELECT 1 FROM daily_stats WHERE target_id=? AND stat_date=?",
            (target_id, today)
        ).fetchone()
        if exists:
            return
        con.execute(
            "INSERT INTO daily_stats(target_id, stat_date, observed, bonus1_present, bonus2_present, recorded_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                target_id,
                today,
                observed,
                _to_int(bonus_presence.get(1)),
                _to_int(bonus_presence.get(2)),
                recorded_at
            )
        )

def db_get_last_check(target_id: int) -> Tuple[Optional[float], Optional[str]]:
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT observed, fetched_at FROM checks WHERE target_id=? ORDER BY fetched_at DESC LIMIT 1",
            (target_id,)
        ).fetchone()
        if not row:
            return None, None
        val = float(row["observed"]) if row["observed"] is not None else None
        ts = row["fetched_at"]
        return val, ts

def db_get_daily_stats(target_id: int) -> List[Dict[str, Optional[float]]]:
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT stat_date, observed, bonus1_present, bonus2_present, recorded_at "
            "FROM daily_stats WHERE target_id=? ORDER BY stat_date",
            (target_id,)
        ).fetchall()
        return [dict(r) for r in rows]

# ---- Sorting helpers ----
class NumItem(QTableWidgetItem):
    """Sort numerically using Qt.UserRole."""
    def __lt__(self, other):
        try:
            return float(self.data(Qt.UserRole)) < float(other.data(Qt.UserRole))
        except Exception:
            return super().__lt__(other)

class TextItem(QTableWidgetItem):
    pass

class BonusCellWidget(QtWidgets.QWidget):
    clear_clicked = QtCore.Signal()

    def __init__(self, marker: str, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)

        self.label = QtWidgets.QLabel()
        self.label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        layout.addWidget(self.label, 1)

        self.button = QtWidgets.QToolButton()
        self.button.setText(marker)
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.setAutoRaise(True)
        self.button.setFocusPolicy(Qt.NoFocus)
        self.button.setVisible(False)
        self.button.clicked.connect(self.clear_clicked.emit)
        layout.addWidget(self.button, 0)

    def set_warning(self, warn: bool):
        if warn:
            self.label.setStyleSheet("color: palette(mid); font-style: italic;")
        else:
            self.label.setStyleSheet("")

class HistoryDialog(QtWidgets.QDialog):
    def __init__(self, parent: Optional[QtWidgets.QWidget], target: Target, stats: List[Dict[str, Optional[float]]]):
        super().__init__(parent)
        self.setWindowTitle(f"Price trend – ID {target.id}")
        self.resize(720, 420)

        layout = QtWidgets.QVBoxLayout(self)

        if not stats:
            label = QtWidgets.QLabel("There are no daily records for this target yet.")
            label.setAlignment(Qt.AlignCenter)
            layout.addWidget(label)
            buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
            buttons.rejected.connect(self.reject)
            buttons.accepted.connect(self.accept)
            layout.addWidget(buttons)
            return

        price_series = QLineSeries()
        price_series.setName("Price")
        price_series.setColor(QtGui.QColor("#2b7cd3"))
        price_series.setPointsVisible(True)
        price_series.setUseOpenGL(False)

        bonus_data: Dict[int, List[Tuple[int, int]]] = {1: [], 2: []}
        x_values: List[int] = []
        price_values: List[float] = []

        for row in stats:
            recorded_at = row.get("recorded_at") or ""
            dt: Optional[datetime]
            try:
                dt = datetime.fromisoformat(recorded_at.replace("Z", "")) if recorded_at else None
            except Exception:
                dt = None
            if dt is None:
                try:
                    dt = datetime.fromisoformat(f"{row.get('stat_date')}T00:00:00")
                except Exception:
                    continue
            qdt = QtCore.QDateTime(dt)
            x = qdt.toMSecsSinceEpoch()
            x_values.append(x)

            observed = row.get("observed")
            if observed is not None:
                try:
                    price = float(observed)
                except Exception:
                    price = None
                if price is not None and math.isfinite(price):
                    price_series.append(x, price)
                    price_values.append(price)

            for idx in (1, 2):
                presence = row.get(f"bonus{idx}_present")
                if presence is None:
                    continue
                try:
                    val_int = int(presence)
                except Exception:
                    continue
                bonus_data[idx].append((x, val_int))

        chart = QChart()
        chart.addSeries(price_series)
        chart.legend().setVisible(True)
        chart.legend().setAlignment(Qt.AlignBottom)
        chart.setAnimationOptions(QChart.NoAnimation)
        chart.setTitle(target.description or target.url)

        axis_x = QDateTimeAxis()
        axis_x.setFormat("dd.MM")
        axis_x.setTitleText("Date")
        chart.addAxis(axis_x, Qt.AlignBottom)
        price_series.attachAxis(axis_x)

        axis_y_price = QValueAxis()
        axis_y_price.setTitleText("Price")
        chart.addAxis(axis_y_price, Qt.AlignLeft)
        price_series.attachAxis(axis_y_price)

        if price_values:
            min_price = min(price_values)
            max_price = max(price_values)
            if math.isclose(min_price, max_price):
                pad = max(1.0, abs(min_price) * 0.1 or 1.0)
                axis_y_price.setRange(min_price - pad, max_price + pad)
            else:
                pad = (max_price - min_price) * 0.1
                axis_y_price.setRange(min_price - pad, max_price + pad)
        else:
            axis_y_price.setRange(0, 1)

        if x_values:
            min_x = min(x_values)
            max_x = max(x_values)
            if min_x == max_x:
                span = 24 * 3600 * 1000
                min_x -= span // 2
                max_x += span // 2
            axis_x.setRange(QtCore.QDateTime.fromMSecsSinceEpoch(min_x), QtCore.QDateTime.fromMSecsSinceEpoch(max_x))
            axis_x.setTickCount(max(2, min(len(set(x_values)) + 1, 8)))

        bonus_axis_added = False
        axis_y_bonus = QValueAxis()
        axis_y_bonus.setRange(-0.1, 1.1)
        axis_y_bonus.setLabelFormat("%d")
        axis_y_bonus.setTitleText("Bonus (1 = yes)")
        axis_y_bonus.setTickCount(3)

        bonus_colors = {
            1: QtGui.QColor("#2e7d32"),
            2: QtGui.QColor("#ad1457")
        }

        for idx in (1, 2):
            points = bonus_data[idx]
            if not points:
                continue
            series = QLineSeries()
            series.setName(f"Bonus {'I' if idx == 1 else 'II'}")
            series.setColor(bonus_colors.get(idx, QtGui.QColor("#555555")))
            series.setPointsVisible(True)
            series.setUseOpenGL(False)
            for x, val in points:
                series.append(x, val)
            chart.addSeries(series)
            series.attachAxis(axis_x)
            if not bonus_axis_added:
                chart.addAxis(axis_y_bonus, Qt.AlignRight)
                bonus_axis_added = True
            series.attachAxis(axis_y_bonus)

        view = QChartView(chart)
        view.setRenderHint(QtGui.QPainter.Antialiasing, True)
        layout.addWidget(view)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

# ---- App core ----
class MainWindow(QMainWindow):
    COL_ID = 0
    COL_URL = 1
    COL_DESC = 2
    COL_BASE = 3
    COL_LAST = 4
    COL_TIME = 5
    COL_DELTA = 6
    COL_BONUS1 = 7
    COL_BONUS2 = 8
    COL_ACTIVE = 9
    COL_ACTIONS = 10

    ROLE_BONUS_HAS_SELECTOR = Qt.UserRole + 10
    ROLE_BONUS_MARK = Qt.UserRole + 11
    BONUS_MARKER = "⚑"

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 700)
        init_db()

        # QSettings
        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.restore_window_geometry()

        top = QWidget()
        top_layout = QVBoxLayout(top)

        # URL bar + Description + Add
        bar = QHBoxLayout()
        self.url_edit = QLineEdit(); self.url_edit.setPlaceholderText("URL… (Shift+Click to pick a number)")
        self.desc_edit = QLineEdit(); self.desc_edit.setPlaceholderText("Description… (optional)")
        btn_add = QPushButton("Add and capture…")
        bar.addWidget(QLabel("URL:")); bar.addWidget(self.url_edit, 4)
        bar.addWidget(QLabel("Description:")); bar.addWidget(self.desc_edit, 2)
        bar.addWidget(btn_add, 1)
        top_layout.addLayout(bar)

        # Table
        self.table = QTableWidget(0, 11)
        self.table.setHorizontalHeaderLabels(["ID", "URL", "Description", "Baseline", "Last", "Fetched", "Change", "Bonus I", "Bonus II", "Active", "Actions"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # Disable selection entirely so the Actions column never gets colored
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setStyleSheet('QTableWidget::item:selected{ background: transparent; }')
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked)
        self.table.setSortingEnabled(True)
        self.table.setColumnHidden(self.COL_ID, True)
        top_layout.addWidget(self.table)

        self._skip_bonus_click = False

        # Bottom
        bottom = QHBoxLayout()
        self.btn_refresh = QPushButton("Check active")
        bottom.addStretch(1)
        bottom.addWidget(self.btn_refresh)
        top_layout.addLayout(bottom)

        self.setCentralWidget(top)

        # ---- Theme menu + auto-detect ----
        menubar = self.menuBar()
        view_menu = menubar.addMenu("Appearance")
        self.actionThemeAuto = view_menu.addAction("Automatic (system)")
        self.actionThemeLight = view_menu.addAction("Light")
        self.actionThemeDark  = view_menu.addAction("Dark")
        self.actionThemeAuto.setCheckable(True)
        self.actionThemeLight.setCheckable(True)
        self.actionThemeDark.setCheckable(True)
        self.theme_group = QtGui.QActionGroup(self)
        for a in (self.actionThemeAuto, self.actionThemeLight, self.actionThemeDark):
            a.setActionGroup(self.theme_group)
            a.setCheckable(True)
        self.actionThemeAuto.triggered.connect(lambda checked=False: self.set_theme('auto'))
        self.actionThemeLight.triggered.connect(lambda checked=False: self.set_theme('light'))
        self.actionThemeDark.triggered.connect(lambda checked=False: self.set_theme('dark'))

        # Signals
        self.url_edit.returnPressed.connect(lambda: asyncio.get_event_loop().create_task(self.on_add()))
        self.desc_edit.returnPressed.connect(lambda: asyncio.get_event_loop().create_task(self.on_add()))
        btn_add.clicked.connect(lambda: asyncio.get_event_loop().create_task(self.on_add()))
        self.btn_refresh.clicked.connect(lambda: asyncio.get_event_loop().create_task(self._refresh_busy()))
        self.table.horizontalHeader().sectionResized.connect(self.save_column_widths)
        self.table.itemChanged.connect(self.on_item_changed)
        self.table.cellClicked.connect(self.on_cell_clicked)

        # Load
        self.reload_table()
        self.restore_column_widths()
        asyncio.get_event_loop().create_task(self.refresh_active())

        # Init theme based on user/system
        self.init_theme()

        self.btn_refresh.setEnabled(True)

    # ---- Theme handling ----
    def detect_system_theme(self) -> str:
        pal = self.palette()
        bg = pal.color(QtGui.QPalette.Window)
        luminance = (0.299*bg.red() + 0.587*bg.green() + 0.114*bg.blue())
        return 'dark' if luminance < 128 else 'light'

    def apply_palette(self, mode: str):
        app = QtWidgets.QApplication.instance()
        pal = app.palette()
        if mode == 'dark':
            pal.setColor(QtGui.QPalette.Window, QtGui.QColor(30,30,30))
            pal.setColor(QtGui.QPalette.Base, QtGui.QColor(22,22,22))
            pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(38,38,38))
            pal.setColor(QtGui.QPalette.Text, QtGui.QColor(230,230,230))
            pal.setColor(QtGui.QPalette.WindowText, QtGui.QColor(230,230,230))
            pal.setColor(QtGui.QPalette.Button, QtGui.QColor(45,45,45))
            pal.setColor(QtGui.QPalette.ButtonText, QtGui.QColor(230,230,230))
            pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor(64,128,255))
            pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor(255,255,255))
        else:
            pal = QtWidgets.QApplication.style().standardPalette()
        app.setPalette(pal)

        self.highlight_colors = {
            'good': QtGui.QColor(0, 200, 0, 90) if mode=='dark' else QtGui.QColor(0, 200, 0, 60),
            'bad':  QtGui.QColor(255, 80, 80, 110) if mode=='dark' else QtGui.QColor(255, 0, 0, 60),
            'same': QtGui.QColor(255,255,255,20) if mode=='dark' else QtGui.QColor(128,128,128,40),
            'err':  QtGui.QColor(255, 200, 0, 120) if mode=='dark' else QtGui.QColor(255, 200, 0, 80),
            'accent_good': QtGui.QColor(0,180,0),
            'accent_bad':  QtGui.QColor(220,0,0),
            'accent_err':  QtGui.QColor(220,160,0),
        }

    def set_theme(self, pref: str):
        self.settings.setValue('theme', pref)
        mode = self.detect_system_theme() if pref == 'auto' else pref
        self.apply_palette(mode)
        self.actionThemeAuto.setChecked(pref=='auto')
        self.actionThemeLight.setChecked(pref=='light')
        self.actionThemeDark.setChecked(pref=='dark')

    def init_theme(self):
        pref = self.settings.value('theme', 'auto')
        if pref not in ('auto','light','dark'):
            pref = 'auto'
        self.set_theme(pref)

    # ---- Persist widths ----
    def save_column_widths(self):
        widths = [self.table.columnWidth(c) for c in range(self.table.columnCount())]
        self.settings.setValue("column_widths", widths)

    def restore_column_widths(self):
        widths = self.settings.value("column_widths")
        if isinstance(widths, list) and widths:
            for i, w in enumerate(widths):
                try:
                    self.table.setColumnWidth(i, int(w))
                except Exception:
                    pass

    def closeEvent(self, event):
        self.save_column_widths()
        self.save_window_geometry()
        super().closeEvent(event)

    def save_window_geometry(self):
        try:
            self.settings.setValue("window_geometry", self.saveGeometry())
        except Exception:
            pass
        try:
            self.settings.setValue("window_maximized", self.isMaximized())
        except Exception:
            pass

    def restore_window_geometry(self):
        geometry = self.settings.value("window_geometry")
        if isinstance(geometry, QtCore.QByteArray) and not geometry.isEmpty():
            self.restoreGeometry(geometry)
        elif isinstance(geometry, (bytes, bytearray)) and geometry:
            self.restoreGeometry(QtCore.QByteArray(geometry))

        maximized = self.settings.value("window_maximized")
        if isinstance(maximized, str):
            maximized = maximized.lower() in {"1", "true", "yes", "y"}
        else:
            maximized = bool(maximized)
        if maximized:
            self.setWindowState(self.windowState() | Qt.WindowMaximized)

    # ---- Table ops ----
    def reload_table(self):
        self.table.setRowCount(0)
        self.targets = db_all_targets()
        for t in self.targets:
            last, ts = db_get_last_check(t.id)
            delta = None if last is None else (last - t.baseline)
            self.add_row(t, last=last, delta=delta, ts=ts)

    def add_row(self, t: Target, last: Optional[float], delta: Optional[float], ts: Optional[str]):
        row = self.table.rowCount()
        self.table.insertRow(row)

        # ID
        id_item = NumItem(str(t.id)); id_item.setData(Qt.UserRole, t.id)
        self.table.setItem(row, self.COL_ID, id_item)
        # URL
        self.table.setItem(row, self.COL_URL, TextItem(t.url))
        # Description (editable)
        desc_item = TextItem(t.description or ""); desc_item.setFlags(desc_item.flags() | Qt.ItemIsEditable)
        self.table.setItem(row, self.COL_DESC, desc_item)
        # Baseline
        base_item = NumItem(str(t.baseline)); base_item.setData(Qt.UserRole, t.baseline)
        self.table.setItem(row, self.COL_BASE, base_item)
        # Last
        last_item = NumItem("-" if last is None else str(last)); last_item.setData(Qt.UserRole, float(last) if last is not None else float('nan'))
        self.table.setItem(row, self.COL_LAST, last_item)
        # Time
        time_item = TextItem("-" if ts is None else self._fmt_time(ts))
        self.table.setItem(row, self.COL_TIME, time_item)
        # Delta
        if delta is None:
            delta_item = NumItem("-"); delta_item.setData(Qt.UserRole, float('nan'))
        else:
            delta_item = NumItem(("+%s" % delta) if delta >= 0 else str(delta))
            delta_item.setData(Qt.UserRole, float(delta))
            if delta != 0:
                font = delta_item.font()
                font.setBold(True)
                delta_item.setFont(font)
            color = QtGui.QBrush(self._delta_color(delta))
            delta_item.setForeground(color)
        self.table.setItem(row, self.COL_DELTA, delta_item)
        # Bonus I
        bonus1_item = TextItem("")
        self.table.setItem(row, self.COL_BONUS1, bonus1_item)
        bonus1_widget = BonusCellWidget(self.BONUS_MARKER, self.table)
        bonus1_widget.clear_clicked.connect(lambda item=bonus1_item: self._on_bonus_clear_clicked(item))
        self.table.setCellWidget(row, self.COL_BONUS1, bonus1_widget)
        self._configure_bonus_item(bonus1_item, t.bonus1_text, bool(t.bonus1_selector))
        # Bonus II
        bonus2_item = TextItem("")
        self.table.setItem(row, self.COL_BONUS2, bonus2_item)
        bonus2_widget = BonusCellWidget(self.BONUS_MARKER, self.table)
        bonus2_widget.clear_clicked.connect(lambda item=bonus2_item: self._on_bonus_clear_clicked(item))
        self.table.setCellWidget(row, self.COL_BONUS2, bonus2_widget)
        self._configure_bonus_item(bonus2_item, t.bonus2_text, bool(t.bonus2_selector))
        # Active
        chk = QCheckBox(); chk.setChecked(bool(t.active))
        chk.stateChanged.connect(lambda state, tid=t.id: db_set_active(tid, 1 if state==Qt.Checked else 0))
        self.table.setCellWidget(row, self.COL_ACTIVE, chk)
        active_item = NumItem("1" if t.active else "0"); active_item.setData(Qt.UserRole, 1 if t.active else 0)
        self.table.setItem(row, self.COL_ACTIVE, active_item)
        # Actions
        cont = QWidget(); h = QtWidgets.QHBoxLayout(cont); h.setContentsMargins(0,0,0,0)
        btn_measure = QPushButton("Check"); btn_delete = QPushButton("Delete")
        h.addWidget(btn_measure); h.addWidget(btn_delete)
        btn_measure.clicked.connect(lambda _=False, b=btn_measure, ti=t, r=row: asyncio.get_event_loop().create_task(self._measure_one_busy(b, ti, r)))
        btn_delete.clicked.connect(lambda _=False, tid=t.id: self.delete_target(tid))
        self.table.setCellWidget(row, self.COL_ACTIONS, cont)

        # Initial color
        self.set_row_color(row, None)

    def _bonus_widget_for_item(self, item: QTableWidgetItem) -> Optional[BonusCellWidget]:
        index = self.table.indexFromItem(item)
        if not index.isValid():
            return None
        widget = self.table.cellWidget(index.row(), index.column())
        return widget if isinstance(widget, BonusCellWidget) else None

    def _configure_bonus_item(self, item: QTableWidgetItem, text_value: Optional[str], has_selector: bool):
        item.setFlags((item.flags() | Qt.ItemIsEnabled) & ~Qt.ItemIsEditable)
        cleaned = (text_value or "").strip()
        is_marker = bool(has_selector and not cleaned)
        item.setData(Qt.UserRole, cleaned)
        item.setData(self.ROLE_BONUS_HAS_SELECTOR, 1 if has_selector else 0)
        item.setData(self.ROLE_BONUS_MARK, 1 if is_marker else 0)
        tooltip = ""
        button_tip = ""
        if has_selector:
            if is_marker:
                tooltip = "Text is defined but was not found."
                button_tip = tooltip + " Click to stop monitoring."
            else:
                tooltip = cleaned
                button_tip = "Click to stop monitoring."
        item.setText(cleaned)
        item.setToolTip(button_tip or tooltip)
        widget = self._bonus_widget_for_item(item)
        if widget:
            widget.label.setText(cleaned)
            widget.label.setToolTip(tooltip)
            widget.button.setVisible(has_selector)
            widget.button.setToolTip(button_tip)
            widget.set_warning(is_marker)
        elif has_selector and is_marker:
            item.setText(self.BONUS_MARKER)
            item.setToolTip(button_tip)

    def _update_bonus_columns(self, row: int, t: Target, bonus_results: Dict[int, Optional[str]]):
        for idx, col in ((1, self.COL_BONUS1), (2, self.COL_BONUS2)):
            item = self.table.item(row, col)
            if item is None:
                continue
            selector = getattr(t, f"bonus{idx}_selector")
            if selector:
                text_value = bonus_results.get(idx)
                setattr(t, f"bonus{idx}_text", text_value)
                self._configure_bonus_item(item, text_value, True)
            else:
                setattr(t, f"bonus{idx}_text", None)
                self._configure_bonus_item(item, None, False)

    def set_row_color(self, row: int, relation: Optional[int], error: bool = False):
        pal = getattr(self, 'highlight_colors', {
            'good': QtGui.QColor(0, 200, 0, 70),
            'bad':  QtGui.QColor(255, 0, 0, 60),
            'same': QtGui.QColor(128,128,128,40),
            'err':  QtGui.QColor(255, 200, 0, 90),
            'accent_good': QtGui.QColor(0,180,0),
            'accent_bad':  QtGui.QColor(220,0,0),
            'accent_err':  QtGui.QColor(220,160,0),
        })
        if error:
            bg = pal['err']; accent = pal['accent_err']
        elif relation is None:
            bg = QtGui.QColor(0,0,0,0); accent = None
        elif relation < 0:
            bg = pal['good']; accent = pal['accent_good']
        elif relation > 0:
            bg = pal['bad'];  accent = pal['accent_bad']
        else:
            bg = pal['same']; accent = None

        for col in range(self.table.columnCount()):
            if col == self.COL_ACTIONS or self.table.cellWidget(row, col) is not None:
                continue
            item = self.table.item(row, col)
            if item is not None:
                item.setBackground(QtGui.QBrush(bg))

        # accent only in ID
        id_item = self.table.item(row, self.COL_ID)
        if id_item and accent:
            id_item.setData(Qt.DecorationRole, self._make_accent_icon(accent))
        elif id_item:
            id_item.setData(Qt.DecorationRole, None)

    def _make_accent_icon(self, color: QtGui.QColor):
        pm = QtGui.QPixmap(6, 16)
        pm.fill(color)
        return QtGui.QIcon(pm)

    def _delta_color(self, delta: float) -> QtGui.QColor:
        highlight = getattr(self, 'highlight_colors', None)
        if delta < 0:
            if isinstance(highlight, dict):
                return highlight.get('accent_good', QtGui.QColor(0, 170, 0))
            return QtGui.QColor(0, 170, 0)
        if delta > 0:
            if isinstance(highlight, dict):
                return highlight.get('accent_bad', QtGui.QColor(220, 0, 0))
            return QtGui.QColor(220, 0, 0)
        return self.palette().color(QtGui.QPalette.WindowText)

    def row_target_id(self, row: int) -> int:
        return int(self.table.item(row, self.COL_ID).text())

    def delete_target(self, target_id: int):
        if QMessageBox.question(self, APP_NAME, f"Delete record {target_id}?") == QMessageBox.Yes:
            db_delete_target(target_id)
            self.reload_table()

    def on_item_changed(self, item: QTableWidgetItem):
        row = item.row()
        tid = self.row_target_id(row)
        if item.column() == self.COL_DESC:
            db_update_description(tid, item.text())

    def on_cell_clicked(self, row: int, column: int):
        if column == self.COL_DELTA:
            tid = self.row_target_id(row)
            target = next((x for x in self.targets if x.id == tid), None)
            if not target:
                return
            stats = db_get_daily_stats(tid)
            dlg = HistoryDialog(self, target, stats)
            dlg.exec()
        elif column in (self.COL_BONUS1, self.COL_BONUS2):
            asyncio.get_event_loop().create_task(self.handle_bonus_click(row, column))

    def _on_bonus_clear_clicked(self, item: QTableWidgetItem):
        index = self.table.indexFromItem(item)
        if not index.isValid():
            return
        row = index.row()
        column = index.column()
        tid = self.row_target_id(row)
        target = next((x for x in self.targets if x.id == tid), None)
        if not target:
            return
        idx = 1 if column == self.COL_BONUS1 else 2
        self._skip_bonus_click = True
        def _reset_skip():
            self._skip_bonus_click = False
        QtCore.QTimer.singleShot(0, _reset_skip)
        db_update_bonus(tid, idx, None, None)
        if idx == 1:
            target.bonus1_selector = None
            target.bonus1_text = None
        else:
            target.bonus2_selector = None
            target.bonus2_text = None
        self._configure_bonus_item(item, None, False)

    async def handle_bonus_click(self, row: int, column: int):
        if self._skip_bonus_click:
            self._skip_bonus_click = False
            return
        item = self.table.item(row, column)
        if item is None:
            return
        tid = self.row_target_id(row)
        target = next((x for x in self.targets if x.id == tid), None)
        if not target:
            return
        idx = 1 if column == self.COL_BONUS1 else 2

        url_item = self.table.item(row, self.COL_URL)
        if url_item is None:
            return
        url = url_item.text()

        self.setEnabled(False)
        try:
            selector, text_value = await capture_text_snippet(url)
            cleaned = text_value.strip()
            stored_text = cleaned or None
            db_update_bonus(tid, idx, selector, stored_text)
            if idx == 1:
                target.bonus1_selector = selector
                target.bonus1_text = stored_text
            else:
                target.bonus2_selector = selector
                target.bonus2_text = stored_text
            self._configure_bonus_item(item, stored_text, True)
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Failed to load text: {e}")
        finally:
            self.setEnabled(True)

    async def _measure_one_busy(self, btn: QtWidgets.QPushButton, t: Target, row: int):
        self._set_button_busy(btn, True)
        try:
            async with HeadlessBrowserManager() as browser_manager:
                await self.measure_one(t, row, browser_manager)
        finally:
            self._set_button_busy(btn, False)

    async def on_add(self):
        url = self.url_edit.text().strip()
        desc = self.desc_edit.text().strip() or None
        if not url:
            return
        self.setEnabled(False)
        try:
            selector, baseline = await capture_target(url)
            new_id = db_insert_target(url, selector, baseline, description=desc)
            self.url_edit.clear(); self.desc_edit.clear()
            self.reload_table()
            QMessageBox.information(self, APP_NAME, f"Saved (ID {new_id})\nBaseline: {baseline}")
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Failed to add: {e}")
        finally:
            self.setEnabled(True)

    def _fmt_time(self, iso: str) -> str:
        try:
            dt = datetime.fromisoformat(iso.replace("Z","")).astimezone()
            return dt.strftime("%d.%m - %H:%M")
        except Exception:
            return iso

    def _set_button_busy(self, btn: QtWidgets.QPushButton, busy: bool):
        if busy:
            btn.setProperty("orig_text", btn.text())
            btn.setText("⏳ " + btn.text())
            btn.setEnabled(False)
            QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
        else:
            orig = btn.property("orig_text")
            if orig:
                btn.setText(orig)
            btn.setEnabled(True)
            QtWidgets.QApplication.restoreOverrideCursor()

    async def measure_one(self, t: Target, row: int, browser_factory: Optional[BrowserFactory] = None):
        timeout = t.timeout_ms if t.timeout_ms else TIMEOUT_MS
        val, ok, details, bonus_results, _newly_filled = await measure_target(t, timeout, browser_factory=browser_factory)
        measurement_failed = (val != val)
        has_issue = bool(details and str(details).lower().startswith("error"))
        if measurement_failed:
            tooltip = details or ""

            last_item = NumItem("-")
            last_item.setData(Qt.UserRole, float('nan'))
            last_item.setToolTip(tooltip)
            self.table.setItem(row, self.COL_LAST, last_item)

            time_item = TextItem("-")
            time_item.setToolTip(tooltip)
            self.table.setItem(row, self.COL_TIME, time_item)

            error_text = "Error"
            delta_item = NumItem(error_text)
            delta_item.setData(Qt.UserRole, float('nan'))
            delta_item.setToolTip(tooltip)

            font = delta_item.font()
            font.setBold(True)
            delta_item.setFont(font)

            highlight = getattr(self, 'highlight_colors', None)
            if isinstance(highlight, dict):
                err_color = highlight.get('accent_err')
            else:
                err_color = None
            if err_color is None:
                err_color = QtGui.QColor(220, 160, 0)
            delta_item.setForeground(QtGui.QBrush(err_color))

            self.table.setItem(row, self.COL_DELTA, delta_item)
            self.set_row_color(row, None, error=True)
            return

        delta = val - t.baseline
        ts = datetime.now(timezone.utc).astimezone().strftime("%d.%m - %H:%M")

        # Last
        last_item = NumItem(str(val))
        last_item.setData(Qt.UserRole, float(val))
        self.table.setItem(row, self.COL_LAST, last_item)
        # Time
        self.table.setItem(row, self.COL_TIME, TextItem(ts))
        # Delta
        if delta != delta:
            delta_item = NumItem("-")
            delta_item.setData(Qt.UserRole, float('nan'))
        else:
            delta_item = NumItem(("+%s" % delta) if delta >= 0 else str(delta))
            delta_item.setData(Qt.UserRole, float(delta))
            if delta != 0:
                font = delta_item.font()
                font.setBold(True)
                delta_item.setFont(font)
            delta_item.setForeground(QtGui.QBrush(self._delta_color(delta)))
        delta_item.setToolTip(details or "")
        self.table.setItem(row, self.COL_DELTA, delta_item)

        relation = -1 if val < t.baseline else (1 if val > t.baseline else 0)
        self.set_row_color(row, relation, error=has_issue)
        if isinstance(bonus_results, dict):
            self._update_bonus_columns(row, t, bonus_results)

    async def refresh_active(self):
        await self.on_refresh()

    async def _refresh_busy(self):
        self._set_button_busy(self.btn_refresh, True)
        try:
            await self.on_refresh()
        finally:
            self._set_button_busy(self.btn_refresh, False)

    async def on_refresh(self):
        # Busy wrapper controls enable/disable
        active_items = []
        for row in range(self.table.rowCount()):
            tid = self.row_target_id(row)
            t = next((x for x in self.targets if x.id == tid), None)
            if not t or not t.active:
                continue
            active_items.append((row, t))

        if not active_items:
            return

        async with HeadlessBrowserManager() as browser_manager:
            for idx, (row, t) in enumerate(active_items):
                await self.measure_one(t, row, browser_manager)
                if idx < len(active_items) - 1:
                    await asyncio.sleep(random.uniform(5, 10))

# ---- Batch ----
async def measure_target(
    t: Target,
    timeout_ms: int,
    browser_factory: Optional[BrowserFactory] = None
) -> Tuple[float, bool, Optional[str], Optional[Dict[int, Optional[str]]], List[int]]:
    try:
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            try:
                val, bonus = await fetch_target_data(t, timeout_ms, browser_factory=browser_factory)
                break
            except Exception as exc:
                last_exc = exc
                if attempt < 1:
                    restart = getattr(browser_factory, "restart", None) if browser_factory else None
                    if restart is not None:
                        try:
                            await restart()
                        except Exception:
                            pass
                    continue
                raise
        else:
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("fetch_target_data failed without raising an exception")

        missing = []
        newly_filled: List[int] = []
        previous_bonus = {
            1: (t.bonus1_text or None),
            2: (t.bonus2_text or None)
        }
        for idx in (1, 2):
            selector = getattr(t, f"bonus{idx}_selector")
            if not selector:
                continue
            text_value = bonus.get(idx)
            db_update_bonus_text(t.id, idx, text_value)
            setattr(t, f"bonus{idx}_text", text_value)
            if not text_value:
                missing.append(idx)
            elif not (previous_bonus.get(idx) or ""):
                newly_filled.append(idx)

        drop_message = None
        if val < t.baseline:
            drop_message = f"Drop from {t.baseline} to {val}"

        details_parts: List[str] = []
        if missing:
            bonus_names = {1: "Bonus I", 2: "Bonus II"}
            details_parts.append("; ".join(f"{bonus_names[m]} text not found" for m in missing))
        if drop_message:
            details_parts.append(drop_message)
        details = "; ".join(details_parts) if details_parts else None
        ok = val >= t.baseline

        db_insert_check(t.id, val, int(ok), details)
        presence_map: Dict[int, Optional[bool]] = {}
        for idx in (1, 2):
            selector = getattr(t, f"bonus{idx}_selector")
            if not selector:
                presence_map[idx] = None
            else:
                presence_map[idx] = bool(bonus.get(idx))

        observed_for_log: Optional[float] = None
        if isinstance(val, (int, float)) and math.isfinite(val):
            observed_for_log = float(val)
        db_log_daily_stat(t.id, observed_for_log, presence_map)

        return val, ok, details, bonus, newly_filled
    except Exception as e:
        db_insert_check(t.id, -1.0, 0, f"Error: {e}")
        return float('nan'), False, f"Error: {e}", None, []

async def run_batch(send_mail_on_drop: bool = True) -> int:
    targets = [t for t in db_all_targets() if t.active]
    if not targets:
        print("[BATCH] No active targets.")
        return 0

    drops = []   # (t, val, details)
    errors = []  # (t, details)
    bonus_hits = []  # (t, idx)

    async with HeadlessBrowserManager() as browser_manager:
        for idx, t in enumerate(targets):
            timeout = t.timeout_ms if t.timeout_ms else TIMEOUT_MS
            val, ok, details, bonus, newly_filled = await measure_target(t, timeout, browser_factory=browser_manager)
            is_error = (val != val) or (details is not None and str(details).lower().startswith("error"))
            status = "OK"
            if is_error:
                status = "ERROR"; errors.append((t, details))
            elif not ok:
                status = "DROP"; drops.append((t, val, details))
            if newly_filled:
                for idx in newly_filled:
                    bonus_hits.append((t, idx))
            print(f"[{status}] id={t.id} {t.url}\n  baseline={t.baseline} observed={val} details={details or '-'}")
            bonus_lines = []
            if isinstance(bonus, dict):
                for idx, label in ((1, "Bonus I"), (2, "Bonus II")):
                    if getattr(t, f"bonus{idx}_selector"):
                        txt = bonus.get(idx)
                        suffix = " (new)" if idx in newly_filled else ""
                        bonus_lines.append(f"{label}={'-' if not txt else txt}{suffix}")
            if bonus_lines:
                print("  " + " | ".join(bonus_lines))

            if idx < len(targets) - 1:
                await asyncio.sleep(random.uniform(5, 10))

    if send_mail_on_drop and (drops or errors or bonus_hits):
        rows_drops = ''.join([
            f"<tr><td>DROP</td><td>{d[0].id}</td><td>{(d[0].description or '')}</td><td>{d[0].url}</td><td>{d[0].baseline}</td><td>{d[1]}</td><td>{d[2] or ''}</td></tr>"
            for d in drops
        ])
        rows_errs = ''.join([
            f"<tr><td>ERROR</td><td>{e[0].id}</td><td>{(e[0].description or '')}</td><td>{e[0].url}</td><td colspan=2>-</td><td>{e[1] or ''}</td></tr>"
            for e in errors
        ])
        rows_bonus = ''.join([
            f"<tr><td>BONUS</td><td>{b[0].id}</td><td>{(b[0].description or '')}</td><td>{b[0].url}</td><td colspan=2>-</td><td>{'Bonus I' if b[1]==1 else 'Bonus II'} found (new)</td></tr>"
            for b in bonus_hits
        ])
        html = f"""
        <h3>{APP_NAME}: measurement report (batch)</h3>
        <p><b>Drops:</b> {len(drops)} &nbsp;|&nbsp; <b>Errors:</b> {len(errors)} &nbsp;|&nbsp; <b>New bonus hits:</b> {len(bonus_hits)}</p>
        <table border=1 cellspacing=0 cellpadding=6>
          <tr><th>Type</th><th>ID</th><th>Description</th><th>URL</th><th>Baseline</th><th>Observed</th><th>Detail</th></tr>
          {rows_drops}{rows_errs}{rows_bonus}
        </table>
        <p>{datetime.now(timezone.utc).isoformat()}</p>
        """
        send_email(f"{APP_NAME} batch: {len(drops)} drop(s), {len(errors)} error(s)", html)

    return 1 if (drops or errors or bonus_hits) else 0

# ---- Entry ----
def run_gui():
    init_db()
    app = QApplication(sys.argv)
    QSettings.setDefaultFormat(QSettings.IniFormat)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationName(APP_NAME)

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    win = MainWindow()
    win.show()

    with loop:
        loop.run_forever()

def main():
    if "--batch" in sys.argv:
        init_db()
        rc = asyncio.run(run_batch(send_mail_on_drop=True))
        sys.exit(rc)
    else:
        run_gui()

if __name__ == "__main__":
    main()
