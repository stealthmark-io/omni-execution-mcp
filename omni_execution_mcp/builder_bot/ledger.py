"""SQLite ledger: groups, traders, bot orders and fills with builder fee.

Attribution is first-touch: a trader belongs to the first group whose deep link
they opened, and every order stores that group so later changes never rewrite
history. Statements only compute the admin share; nothing here moves funds.
"""

from __future__ import annotations

import calendar
import secrets
import sqlite3
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Iterable, List, Mapping, Optional, Tuple

ADMIN_SHARE_BPS_DEFAULT = 5000
USD_QUANT = Decimal("0.000001")

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    admin_tg_id INTEGER NOT NULL,
    payout_address TEXT,
    share_bps INTEGER NOT NULL,
    created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS traders (
    tg_id INTEGER PRIMARY KEY,
    group_id TEXT REFERENCES groups(id),
    hl_address TEXT,
    agent_address TEXT,
    agent_keystore TEXT,
    created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    hl_address TEXT NOT NULL,
    oid INTEGER NOT NULL,
    tg_id INTEGER NOT NULL,
    group_id TEXT,
    coin TEXT NOT NULL,
    is_buy INTEGER NOT NULL,
    builder_tenths_bps INTEGER NOT NULL,
    created_ms INTEGER NOT NULL,
    PRIMARY KEY (hl_address, oid)
);
CREATE TABLE IF NOT EXISTS fills (
    hl_address TEXT NOT NULL,
    tid INTEGER NOT NULL,
    oid INTEGER NOT NULL,
    coin TEXT NOT NULL,
    px TEXT NOT NULL,
    sz TEXT NOT NULL,
    builder_fee TEXT NOT NULL,
    fee_token TEXT NOT NULL,
    time_ms INTEGER NOT NULL,
    PRIMARY KEY (hl_address, tid)
);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def month_bounds_ms(year: int, month: int) -> Tuple[int, int]:
    """[start, end) of a UTC calendar month in epoch milliseconds."""
    if not 1 <= month <= 12:
        raise ValueError(f"Mes inválido: {month}")
    start = calendar.timegm((year, month, 1, 0, 0, 0))
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    end = calendar.timegm((next_year, next_month, 1, 0, 0, 0))
    return start * 1000, end * 1000


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    admin_tg_id: int
    payout_address: Optional[str]
    share_bps: int


@dataclass(frozen=True)
class Trader:
    tg_id: int
    group_id: Optional[str]
    hl_address: Optional[str]
    agent_address: Optional[str]
    agent_keystore: Optional[str]


@dataclass(frozen=True)
class Statement:
    group_id: str
    start_ms: int
    end_ms: int
    fills: int
    volume_usd: Decimal
    builder_fees_usd: Decimal
    admin_share_usd: Decimal
    operator_net_usd: Decimal
    share_bps: int
    payout_address: Optional[str]


class Ledger:
    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path)
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def create_group(
        self,
        name: str,
        admin_tg_id: int,
        share_bps: int = ADMIN_SHARE_BPS_DEFAULT,
    ) -> Group:
        name = name.strip()
        if not name:
            raise ValueError("El grupo necesita un nombre.")
        if not 0 <= share_bps <= 10000:
            raise ValueError(f"share_bps fuera de rango: {share_bps}")
        while True:
            group_id = secrets.token_hex(4)
            try:
                with self._db:
                    self._db.execute(
                        "INSERT INTO groups (id, name, admin_tg_id, payout_address, share_bps, created_ms)"
                        " VALUES (?, ?, ?, NULL, ?, ?)",
                        (group_id, name, admin_tg_id, share_bps, _now_ms()),
                    )
                break
            except sqlite3.IntegrityError:
                continue
        group = self.get_group(group_id)
        assert group is not None
        return group

    def get_group(self, group_id: str) -> Optional[Group]:
        row = self._db.execute(
            "SELECT id, name, admin_tg_id, payout_address, share_bps FROM groups WHERE id = ?",
            (group_id,),
        ).fetchone()
        return Group(*row) if row else None

    def groups_for_admin(self, admin_tg_id: int) -> List[Group]:
        rows = self._db.execute(
            "SELECT id, name, admin_tg_id, payout_address, share_bps FROM groups"
            " WHERE admin_tg_id = ? ORDER BY created_ms",
            (admin_tg_id,),
        ).fetchall()
        return [Group(*row) for row in rows]

    def set_payout_address(self, group_id: str, payout_address: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE groups SET payout_address = ? WHERE id = ?",
                (payout_address.lower(), group_id),
            )

    def group_trader_addresses(self, group_id: str) -> List[str]:
        rows = self._db.execute(
            "SELECT DISTINCT hl_address FROM orders WHERE group_id = ? ORDER BY hl_address",
            (group_id,),
        ).fetchall()
        return [row[0] for row in rows]

    def count_group_traders(self, group_id: str) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) FROM traders WHERE group_id = ?", (group_id,)
        ).fetchone()
        return int(row[0])

    def register_trader(self, tg_id: int, group_id: Optional[str] = None) -> Trader:
        """Creates the trader if new; assigns a group only if it has none yet."""
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO traders (tg_id, group_id, created_ms) VALUES (?, ?, ?)",
                (tg_id, group_id, _now_ms()),
            )
            if group_id:
                self._db.execute(
                    "UPDATE traders SET group_id = ? WHERE tg_id = ? AND group_id IS NULL",
                    (group_id, tg_id),
                )
        trader = self.get_trader(tg_id)
        assert trader is not None
        return trader

    def get_trader(self, tg_id: int) -> Optional[Trader]:
        row = self._db.execute(
            "SELECT tg_id, group_id, hl_address, agent_address, agent_keystore FROM traders"
            " WHERE tg_id = ?",
            (tg_id,),
        ).fetchone()
        return Trader(*row) if row else None

    def link_wallet(self, tg_id: int, hl_address: str) -> Trader:
        """Links the public main address; a different address drops the old agent."""
        address = hl_address.lower()
        self.register_trader(tg_id)
        with self._db:
            self._db.execute(
                "UPDATE traders SET hl_address = ?, agent_address = NULL, agent_keystore = NULL"
                " WHERE tg_id = ? AND (hl_address IS NULL OR hl_address != ?)",
                (address, tg_id, address),
            )
        trader = self.get_trader(tg_id)
        assert trader is not None
        return trader

    def set_agent(self, tg_id: int, agent_address: str, agent_keystore: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE traders SET agent_address = ?, agent_keystore = ? WHERE tg_id = ?",
                (agent_address.lower(), agent_keystore, tg_id),
            )

    def record_order(
        self,
        hl_address: str,
        oid: int,
        tg_id: int,
        group_id: Optional[str],
        coin: str,
        is_buy: bool,
        builder_tenths_bps: int,
    ) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO orders"
                " (hl_address, oid, tg_id, group_id, coin, is_buy, builder_tenths_bps, created_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (hl_address.lower(), int(oid), tg_id, group_id, coin, int(is_buy),
                 builder_tenths_bps, _now_ms()),
            )

    def ingest_fills(self, hl_address: str, fills: Iterable[Mapping]) -> int:
        """Stores fills of orders this bot placed; returns how many were new."""
        address = hl_address.lower()
        own_oids = {
            row[0]
            for row in self._db.execute(
                "SELECT oid FROM orders WHERE hl_address = ?", (address,)
            )
        }
        inserted = 0
        with self._db:
            for fill in fills:
                oid = int(fill["oid"])
                if oid not in own_oids:
                    continue
                cursor = self._db.execute(
                    "INSERT OR IGNORE INTO fills"
                    " (hl_address, tid, oid, coin, px, sz, builder_fee, fee_token, time_ms)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        address,
                        int(fill["tid"]),
                        oid,
                        str(fill["coin"]),
                        str(Decimal(str(fill["px"]))),
                        str(Decimal(str(fill["sz"]))),
                        str(Decimal(str(fill.get("builderFee", "0")))),
                        str(fill["feeToken"]),
                        int(fill["time"]),
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def statement(self, group_id: str, start_ms: int, end_ms: int) -> Statement:
        group = self.get_group(group_id)
        if group is None:
            raise ValueError(f"Grupo inexistente: {group_id}")
        rows = self._db.execute(
            "SELECT f.px, f.sz, f.builder_fee FROM fills f"
            " JOIN orders o ON o.hl_address = f.hl_address AND o.oid = f.oid"
            " WHERE o.group_id = ? AND f.time_ms >= ? AND f.time_ms < ? AND f.fee_token = 'USDC'",
            (group_id, start_ms, end_ms),
        ).fetchall()
        volume = sum((Decimal(px) * Decimal(sz) for px, sz, _ in rows), Decimal(0))
        fees = sum((Decimal(fee) for _, _, fee in rows), Decimal(0))
        admin_share = (fees * group.share_bps / Decimal(10000)).quantize(USD_QUANT, rounding=ROUND_DOWN)
        return Statement(
            group_id=group_id,
            start_ms=start_ms,
            end_ms=end_ms,
            fills=len(rows),
            volume_usd=volume.quantize(USD_QUANT),
            builder_fees_usd=fees.quantize(USD_QUANT),
            admin_share_usd=admin_share,
            operator_net_usd=(fees - admin_share).quantize(USD_QUANT),
            share_bps=group.share_bps,
            payout_address=group.payout_address,
        )
