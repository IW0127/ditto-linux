"""SQLite clip store using the same schema as Windows Ditto (Main / Data / Types / CopyBuffers)."""
from __future__ import annotations

import base64
import json
import shlex
import sqlite3
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import formats as fmt

NO_PARENT = -1

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS Main(
        lID INTEGER PRIMARY KEY AUTOINCREMENT, lDate INTEGER, mText TEXT, lShortCut INTEGER,
        lDontAutoDelete INTEGER, CRC INTEGER, bIsGroup INTEGER, lParentID INTEGER,
        QuickPasteText TEXT, clipOrder REAL, clipGroupOrder REAL, globalShortCut INTEGER,
        lastPasteDate INTEGER, stickyClipOrder REAL, stickyClipGroupOrder REAL,
        MoveToGroupShortCut INTEGER, GlobalMoveToGroupShortCut INTEGER)""",
    """CREATE TABLE IF NOT EXISTS Data(
        lID INTEGER PRIMARY KEY AUTOINCREMENT, lParentID INTEGER,
        strClipBoardFormat TEXT, ooData BLOB)""",
    "CREATE TABLE IF NOT EXISTS Types(lID INTEGER PRIMARY KEY AUTOINCREMENT, TypeText TEXT)",
    "CREATE TABLE IF NOT EXISTS CopyBuffers(lID INTEGER PRIMARY KEY AUTOINCREMENT, lClipID INTEGER, lCopyBuffer INTEGER)",
    "CREATE TABLE IF NOT EXISTS MainDeletes(clipID INTEGER, modifiedDate)",
    """CREATE TRIGGER IF NOT EXISTS delete_data_trigger BEFORE DELETE ON Main FOR EACH ROW
        BEGIN INSERT INTO MainDeletes VALUES(old.lID, datetime('now')); END""",
    """CREATE TRIGGER IF NOT EXISTS MainDeletes_delete_data_trigger BEFORE DELETE ON MainDeletes FOR EACH ROW
        BEGIN DELETE FROM CopyBuffers WHERE lClipID = old.clipID;
              DELETE FROM Data WHERE lParentID = old.clipID; END""",
    "CREATE INDEX IF NOT EXISTS Data_ParentId_Format ON Data(lParentID COLLATE BINARY ASC, strClipBoardFormat COLLATE NOCASE ASC)",
    "CREATE INDEX IF NOT EXISTS Main_TopLevel ON Main(stickyClipOrder DESC, bIsGroup ASC, clipOrder DESC)",
    "CREATE INDEX IF NOT EXISTS Main_InGroup2 ON Main(lParentId ASC, stickyClipGroupOrder DESC, bIsGroup ASC, clipGroupOrder DESC)",
    "CREATE INDEX IF NOT EXISTS Main_CRC on Main(CRC ASC)",
    # ditto-linux extension (Windows Ditto ignores this table): clips whose content is hidden in
    # the list, e.g. passwords, so only their title is shown.
    "CREATE TABLE IF NOT EXISTS ClipFlags(lClipID INTEGER PRIMARY KEY, bHidden INTEGER DEFAULT 0, sSource TEXT)",
    """CREATE TRIGGER IF NOT EXISTS ClipFlags_delete_trigger AFTER DELETE ON Main FOR EACH ROW
        BEGIN DELETE FROM ClipFlags WHERE lClipID = old.lID; END""",
]

# Columns added over Ditto's history; older databases are upgraded in place.
_UPGRADE_COLUMNS = {
    "clipOrder": "REAL", "clipGroupOrder": "REAL", "globalShortCut": "INTEGER",
    "lastPasteDate": "INTEGER", "stickyClipOrder": "REAL", "stickyClipGroupOrder": "REAL",
    "MoveToGroupShortCut": "INTEGER", "GlobalMoveToGroupShortCut": "INTEGER",
}

DEFAULT_TYPES = ("CF_UNICODETEXT", "HTML Format", "PNG", "text/uri-list", "CF_TEXT", "CF_DIB")


@dataclass
class Clip:
    id: int
    text: str
    date: int
    is_group: bool
    parent_id: int
    sticky: bool
    never_delete: bool
    description: str
    last_paste: int | None
    hidden: bool = False
    source: str = ""      # app the clip was copied from, when known

    MASK = "••••••••"

    @property
    def title(self) -> str:
        if self.hidden:
            return self.description or self.MASK
        return self.description or self.text

    def to_dict(self) -> dict:
        return {
            "id": self.id, "text": self.text, "date": self.date, "is_group": self.is_group,
            "parent_id": self.parent_id, "sticky": self.sticky, "never_delete": self.never_delete,
            "description": self.description, "last_paste": self.last_paste,
            "hidden": self.hidden, "source": self.source,
        }


_COLS = ("lID, mText, lDate, bIsGroup, lParentID, stickyClipOrder, stickyClipGroupOrder, "
         "lDontAutoDelete, QuickPasteText, lastPasteDate, "
         "COALESCE((SELECT bHidden FROM ClipFlags WHERE lClipID = Main.lID), 0), "
         "(SELECT sSource FROM ClipFlags WHERE lClipID = Main.lID)")


def _row_to_clip(r) -> Clip:
    parent = r[4] if r[4] is not None else NO_PARENT
    sticky = (r[5] is not None) if parent in (NO_PARENT, 0) else (r[6] is not None)
    return Clip(id=r[0], text=r[1] or "", date=r[2] or 0, is_group=bool(r[3]), parent_id=parent,
                sticky=sticky, never_delete=bool(r[7]), description=r[8] or "", last_paste=r[9],
                hidden=bool(r[10]), source=r[11] or "")


def compute_crc(formats: dict[str, bytes]) -> int:
    crc = 0
    for mime in sorted(formats):
        crc = zlib.crc32(mime.encode(), crc)
        crc = zlib.crc32(formats[mime], crc)
    # Ditto stores CRC as a signed 32 bit integer
    return crc - (1 << 32) if crc >= (1 << 31) else crc


class ClipStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA busy_timeout = 10000")
        if str(path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    # ------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        with self._lock, self._tx():
            existing = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "Main" in existing:
                cols = {r[1] for r in self.conn.execute("PRAGMA table_info(Main)")}
                for col, typ in _UPGRADE_COLUMNS.items():
                    if col not in cols:
                        self.conn.execute(f"ALTER TABLE Main ADD {col} {typ}")
            for stmt in _SCHEMA:
                self.conn.execute(stmt)
            if "sSource" not in {r[1] for r in self.conn.execute("PRAGMA table_info(ClipFlags)")}:
                self.conn.execute("ALTER TABLE ClipFlags ADD sSource TEXT")
            if not self.conn.execute("SELECT 1 FROM Types LIMIT 1").fetchone():
                self.conn.executemany("INSERT INTO Types(TypeText) VALUES(?)", [(t,) for t in DEFAULT_TYPES])

    def _tx(self):
        store = self

        class _Tx:
            def __enter__(self):
                store.conn.execute("BEGIN IMMEDIATE")

            def __exit__(self, exc_type, *_):
                store.conn.execute("ROLLBACK" if exc_type else "COMMIT")
                return False

        return _Tx()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def data_version(self) -> int:
        """Changes whenever another connection commits; used to refresh the UI."""
        return self.conn.execute("PRAGMA data_version").fetchone()[0]

    # ------------------------------------------------------------------ helpers
    def _next_order(self, column: str, parent: int | None = None) -> float:
        if parent is None:
            row = self.conn.execute(f"SELECT MAX({column}) FROM Main").fetchone()
        else:
            row = self.conn.execute(f"SELECT MAX({column}) FROM Main WHERE lParentID = ?", (parent,)).fetchone()
        return (row[0] or 0) + 1

    def _purge_deleted(self) -> None:
        # Fires MainDeletes_delete_data_trigger, removing Data and CopyBuffers rows.
        self.conn.execute("DELETE FROM MainDeletes")

    # ------------------------------------------------------------------ clips
    def add_clip(self, formats: dict[str, bytes], move_duplicate_to_top: bool = True,
                 parent_id: int = NO_PARENT, source: str = "") -> tuple[int, bool]:
        """Store a clip. Returns ``(clip_id, created)``; duplicates are moved to the top instead."""
        formats = {k: v for k, v in formats.items() if v}
        if not formats:
            raise ValueError("clip has no data")
        crc = compute_crc(formats)
        text = fmt.describe(formats)
        now = int(time.time())
        with self._lock, self._tx():
            dup = self.conn.execute(
                "SELECT lID FROM Main WHERE CRC = ? AND bIsGroup = 0 ORDER BY clipOrder DESC LIMIT 1", (crc,)
            ).fetchone()
            if dup and self._formats_locked(dup[0]) == formats:
                if move_duplicate_to_top:
                    self.conn.execute("UPDATE Main SET clipOrder = ?, lDate = ? WHERE lID = ?",
                                      (self._next_order("clipOrder"), now, dup[0]))
                if source:
                    self._set_source(dup[0], source)
                return dup[0], False
            cur = self.conn.execute(
                "INSERT INTO Main(lDate, mText, lShortCut, lDontAutoDelete, CRC, bIsGroup, lParentID, "
                "QuickPasteText, clipOrder, clipGroupOrder, globalShortCut, MoveToGroupShortCut, "
                "GlobalMoveToGroupShortCut) VALUES(?, ?, 0, 0, ?, 0, ?, '', ?, ?, 0, 0, 0)",
                (now, text, crc, parent_id, self._next_order("clipOrder"),
                 self._next_order("clipGroupOrder", parent_id) if parent_id != NO_PARENT else 0),
            )
            clip_id = cur.lastrowid
            self.conn.executemany(
                "INSERT INTO Data(lParentID, strClipBoardFormat, ooData) VALUES(?, ?, ?)",
                [(clip_id, name, sqlite3.Binary(data)) for name, data in fmt.to_storage(formats)],
            )
            if source:
                self._set_source(clip_id, source)
            return clip_id, True

    def _set_source(self, clip_id: int, source: str) -> None:
        self.conn.execute("INSERT INTO ClipFlags(lClipID, sSource) VALUES(?, ?) "
                          "ON CONFLICT(lClipID) DO UPDATE SET sSource = excluded.sSource", (clip_id, source))

    def recent_sources(self, limit: int = 20) -> list[str]:
        """Apps that clips were recently copied from (for the 'ignore apps' option)."""
        with self._lock:
            return [r[0] for r in self.conn.execute(
                "SELECT sSource FROM ClipFlags WHERE sSource IS NOT NULL AND sSource != '' "
                "GROUP BY sSource ORDER BY MAX(lClipID) DESC LIMIT ?", (limit,))]

    def add_text(self, text: str, **kw) -> tuple[int, bool]:
        return self.add_clip({fmt.TEXT: text.encode("utf-8")}, **kw)

    def get(self, clip_id: int) -> Clip | None:
        with self._lock:
            r = self.conn.execute(f"SELECT {_COLS} FROM Main WHERE lID = ?", (clip_id,)).fetchone()
        return _row_to_clip(r) if r else None

    def _formats_locked(self, clip_id: int) -> dict[str, bytes]:
        rows = self.conn.execute(
            "SELECT strClipBoardFormat, ooData FROM Data WHERE lParentID = ? ORDER BY lID", (clip_id,)
        ).fetchall()
        return fmt.from_storage(rows)

    def get_formats(self, clip_id: int) -> dict[str, bytes]:
        with self._lock:
            return self._formats_locked(clip_id)

    def list(self, parent_id: int = NO_PARENT, search: str = "", limit: int = 500,
             offset: int = 0, groups_only: bool = False) -> list[Clip]:
        """Clips ordered like Ditto: sticky first, then most recent.

        At the top level (``parent_id == -1``) every non-group clip is shown plus top level groups;
        searching at the top level searches all clips.
        """
        where, args = [], []
        top = parent_id == NO_PARENT
        if groups_only:
            where.append("bIsGroup = 1")
        if search:
            for term in _split_terms(search):
                where.append("((mText LIKE ? ESCAPE '\\' AND lID NOT IN (SELECT lClipID FROM ClipFlags WHERE bHidden = 1))"
                             " OR QuickPasteText LIKE ? ESCAPE '\\')")
                like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                args += [like, like]
        if top:
            if not search and not groups_only:
                where.append("(bIsGroup = 0 OR lParentID = -1 OR lParentID = 0 OR lParentID IS NULL)")
            order = "stickyClipOrder IS NULL, stickyClipOrder DESC, bIsGroup DESC, clipOrder DESC"
        else:
            where.append("lParentID = ?")
            args.append(parent_id)
            order = "stickyClipGroupOrder IS NULL, stickyClipGroupOrder DESC, bIsGroup DESC, clipGroupOrder DESC"
        sql = f"SELECT {_COLS} FROM Main"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        with self._lock:
            rows = self.conn.execute(sql, args + [limit, offset]).fetchall()
        return [_row_to_clip(r) for r in rows]

    def count(self, include_groups: bool = False) -> int:
        sql = "SELECT COUNT(*) FROM Main" + ("" if include_groups else " WHERE bIsGroup = 0")
        with self._lock:
            return self.conn.execute(sql).fetchone()[0]

    def delete(self, ids: Iterable[int]) -> int:
        """Delete clips; deleting a group deletes everything inside it."""
        ids = list(ids)
        removed = 0
        with self._lock, self._tx():
            todo = list(ids)
            while todo:
                cid = todo.pop()
                todo += [r[0] for r in self.conn.execute("SELECT lID FROM Main WHERE lParentID = ? AND lID != ?",
                                                         (cid, cid))]
                removed += self.conn.execute("DELETE FROM Main WHERE lID = ?", (cid,)).rowcount
            self._purge_deleted()
        return removed

    def clear_history(self) -> int:
        """Delete every clip that is not a group, not in a group and not protected."""
        with self._lock:
            ids = [r[0] for r in self.conn.execute(
                "SELECT lID FROM Main WHERE bIsGroup = 0 AND (lDontAutoDelete = 0 OR lDontAutoDelete IS NULL) "
                "AND (lParentID IN (-1, 0) OR lParentID IS NULL)")]
        return self.delete(ids)

    def set_sticky(self, clip_id: int, sticky: bool) -> None:
        clip = self.get(clip_id)
        if not clip:
            raise KeyError(clip_id)
        col = "stickyClipOrder" if clip.parent_id in (NO_PARENT, 0) else "stickyClipGroupOrder"
        with self._lock, self._tx():
            value = self._next_order(col) if sticky else None
            self.conn.execute(f"UPDATE Main SET {col} = ? WHERE lID = ?", (value, clip_id))

    def move_sticky(self, clip_id: int, delta: int) -> bool:
        """Move a pinned (sticky) clip up (delta < 0) or down among the pinned clips of its level.

        ``delta`` of ``-10**9`` moves it to the very top. Returns False if it could not move.
        """
        clip = self.get(clip_id)
        if not clip or not clip.sticky:
            return False
        top = clip.parent_id in (NO_PARENT, 0)
        col = "stickyClipOrder" if top else "stickyClipGroupOrder"
        scope = "(lParentID IN (-1, 0) OR lParentID IS NULL)" if top else "lParentID = ?"
        args = () if top else (clip.parent_id,)
        with self._lock, self._tx():
            ids = [r[0] for r in self.conn.execute(
                f"SELECT lID FROM Main WHERE {col} IS NOT NULL AND {scope} ORDER BY {col} DESC", args)]
            i = ids.index(clip_id)
            j = max(0, min(len(ids) - 1, i + delta))
            if i == j:
                return False
            ids.insert(j, ids.pop(i))
            n = len(ids)
            self.conn.executemany(f"UPDATE Main SET {col} = ? WHERE lID = ?",
                                  [(float(n - k), cid) for k, cid in enumerate(ids)])
        return True

    def set_hidden(self, clip_id: int, hidden: bool) -> None:
        """Hide a clip's content in the list (only its title is shown); it still pastes normally."""
        with self._lock:
            self.conn.execute("INSERT INTO ClipFlags(lClipID, bHidden) VALUES(?, ?) "
                              "ON CONFLICT(lClipID) DO UPDATE SET bHidden = excluded.bHidden",
                              (clip_id, int(hidden)))

    def pin(self, clip_id: int, title: str | None = None, hidden: bool | None = None) -> None:
        """Pin a clip at the top, optionally giving it a title and hiding its content."""
        clip = self.get(clip_id)
        if not clip:
            raise KeyError(clip_id)
        if not clip.sticky:
            self.set_sticky(clip_id, True)
        if title is not None:
            self.set_description(clip_id, title)
        if hidden is not None:
            self.set_hidden(clip_id, hidden)

    def set_never_delete(self, clip_id: int, value: bool) -> None:
        with self._lock:
            self.conn.execute("UPDATE Main SET lDontAutoDelete = ? WHERE lID = ?",
                              (int(time.time()) if value else 0, clip_id))

    def set_description(self, clip_id: int, description: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE Main SET QuickPasteText = ? WHERE lID = ?", (description, clip_id))

    def update_text(self, clip_id: int, text: str) -> None:
        """Replace a clip's contents with plain text (Ditto's "Edit clip")."""
        formats = {fmt.TEXT: text.encode("utf-8")}
        with self._lock, self._tx():
            self.conn.execute("DELETE FROM Data WHERE lParentID = ?", (clip_id,))
            self.conn.executemany(
                "INSERT INTO Data(lParentID, strClipBoardFormat, ooData) VALUES(?, ?, ?)",
                [(clip_id, n, sqlite3.Binary(d)) for n, d in fmt.to_storage(formats)])
            self.conn.execute("UPDATE Main SET mText = ?, CRC = ? WHERE lID = ?",
                              (text, compute_crc(formats), clip_id))

    def mark_pasted(self, clip_id: int, move_to_top: bool = True) -> None:
        with self._lock, self._tx():
            sets, args = ["lastPasteDate = ?"], [int(time.time())]
            clip = self.conn.execute("SELECT lParentID FROM Main WHERE lID = ?", (clip_id,)).fetchone()
            if clip and move_to_top:
                sets.append("clipOrder = ?")
                args.append(self._next_order("clipOrder"))
            self.conn.execute(f"UPDATE Main SET {', '.join(sets)} WHERE lID = ?", args + [clip_id])

    # ------------------------------------------------------------------ groups
    def create_group(self, name: str, parent_id: int = NO_PARENT) -> int:
        with self._lock, self._tx():
            cur = self.conn.execute(
                "INSERT INTO Main(lDate, mText, lShortCut, lDontAutoDelete, CRC, bIsGroup, lParentID, "
                "QuickPasteText, clipOrder, clipGroupOrder) VALUES(?, ?, 0, ?, 0, 1, ?, '', ?, ?)",
                (int(time.time()), name, int(time.time()), parent_id, self._next_order("clipOrder"),
                 self._next_order("clipGroupOrder", parent_id)))
            return cur.lastrowid

    def rename_group(self, group_id: int, name: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE Main SET mText = ? WHERE lID = ? AND bIsGroup = 1", (name, group_id))

    def move_to_group(self, ids: Iterable[int], group_id: int) -> None:
        with self._lock, self._tx():
            if group_id != NO_PARENT:
                g = self.conn.execute("SELECT bIsGroup FROM Main WHERE lID = ?", (group_id,)).fetchone()
                if not g or not g[0]:
                    raise ValueError(f"{group_id} is not a group")
            for cid in ids:
                if cid == group_id:
                    continue
                order = self._next_order("clipGroupOrder", group_id) if group_id != NO_PARENT else 0
                self.conn.execute(
                    "UPDATE Main SET lParentID = ?, clipGroupOrder = ?, stickyClipGroupOrder = NULL WHERE lID = ?",
                    (group_id, order, cid))

    def groups(self) -> list[Clip]:
        with self._lock:
            rows = self.conn.execute(f"SELECT {_COLS} FROM Main WHERE bIsGroup = 1 ORDER BY mText COLLATE NOCASE")
            return [_row_to_clip(r) for r in rows]

    # ------------------------------------------------------------------ maintenance
    def cleanup(self, max_clips: int = 0, expire_days: int = 0) -> int:
        """Ditto's auto-delete: keeps groups, clips inside groups, sticky and "never delete" clips."""
        removable = ("bIsGroup = 0 AND (lDontAutoDelete = 0 OR lDontAutoDelete IS NULL) "
                     "AND stickyClipOrder IS NULL AND (lParentID IN (-1, 0) OR lParentID IS NULL)")
        ids: set[int] = set()
        with self._lock:
            if expire_days > 0:
                cutoff = int(time.time()) - expire_days * 86400
                ids |= {r[0] for r in self.conn.execute(
                    f"SELECT lID FROM Main WHERE {removable} AND lDate < ? "
                    "AND (lastPasteDate IS NULL OR lastPasteDate < ?)", (cutoff, cutoff))}
            if max_clips > 0:
                ids |= {r[0] for r in self.conn.execute(
                    f"SELECT lID FROM Main WHERE {removable} ORDER BY clipOrder DESC LIMIT -1 OFFSET ?",
                    (max_clips,))}
        return self.delete(ids) if ids else 0

    def vacuum(self) -> None:
        with self._lock:
            self.conn.execute("VACUUM")

    # ------------------------------------------------------------------ import / export
    def export_json(self) -> dict:
        with self._lock:
            rows = self.conn.execute(
                f"SELECT {_COLS}, clipOrder, clipGroupOrder FROM Main ORDER BY lID").fetchall()
            clips = []
            for r in rows:
                c = _row_to_clip(r).to_dict()
                c["order"], c["group_order"] = r[-2], r[-1]
                c["formats"] = {k: base64.b64encode(v).decode() for k, v in self._formats_locked(r[0]).items()}
                clips.append(c)
        return {"format": "ditto-linux-export", "version": 1, "clips": clips}

    def import_json(self, data: dict) -> int:
        if data.get("format") != "ditto-linux-export":
            raise ValueError("not a ditto-linux export file")
        id_map: dict[int, int] = {}
        clips = sorted(data.get("clips", []), key=lambda c: (not c.get("is_group"), c.get("order") or 0))
        imported = 0
        pending_parents = []
        for c in clips:
            if c.get("is_group"):
                new_id = self.create_group(c.get("text", "Group"))
            else:
                formats = {k: base64.b64decode(v) for k, v in c.get("formats", {}).items()}
                if not formats:
                    continue
                new_id, created = self.add_clip(formats)
                if not created:
                    id_map[c["id"]] = new_id
                    continue
            id_map[c["id"]] = new_id
            imported += 1
            if c.get("description"):
                self.set_description(new_id, c["description"])
            if c.get("never_delete"):
                self.set_never_delete(new_id, True)
            if c.get("hidden"):
                self.set_hidden(new_id, True)
            if c.get("sticky") and not c.get("is_group") and c.get("parent_id", NO_PARENT) in (NO_PARENT, 0, None):
                self.set_sticky(new_id, True)
            if c.get("parent_id", NO_PARENT) not in (NO_PARENT, 0, None):
                pending_parents.append((new_id, c["parent_id"]))
        for new_id, old_parent in pending_parents:
            if old_parent in id_map:
                self.move_to_group([new_id], id_map[old_parent])
        return imported

    def stats(self) -> dict:
        with self._lock:
            clips = self.conn.execute("SELECT COUNT(*) FROM Main WHERE bIsGroup = 0").fetchone()[0]
            groups = self.conn.execute("SELECT COUNT(*) FROM Main WHERE bIsGroup = 1").fetchone()[0]
            size = self.conn.execute("SELECT COALESCE(SUM(LENGTH(ooData)), 0) FROM Data").fetchone()[0]
        return {"clips": clips, "groups": groups, "data_bytes": size, "path": str(self.path)}


def _split_terms(search: str) -> list[str]:
    try:
        return [t for t in shlex.split(search) if t]
    except ValueError:
        return [t for t in search.split() if t]


def dumps_export(store: ClipStore) -> str:
    return json.dumps(store.export_json(), indent=1)
