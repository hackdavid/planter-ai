"""SQLite cache database helper for venue discovery."""

import sqlite3
import hashlib
import json
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta


class CacheDB:
    """Manages SQLite persistence for raw and filtered venue data."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS scan_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cache_hash TEXT UNIQUE NOT NULL,
                    query TEXT NOT NULL,
                    categories TEXT NOT NULL,
                    quantity_target INTEGER NOT NULL,
                    raw_found INTEGER DEFAULT 0,
                    filtered_count INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS raw_venues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_session_id INTEGER NOT NULL,
                    place_id TEXT NOT NULL,
                    name TEXT,
                    lat REAL,
                    lng REAL,
                    address TEXT,
                    types TEXT,
                    user_ratings_total INTEGER,
                    business_status TEXT,
                    discovery_source TEXT,
                    grid_point_lat REAL,
                    grid_point_lng REAL,
                    UNIQUE(scan_session_id, place_id),
                    FOREIGN KEY (scan_session_id) REFERENCES scan_sessions(id)
                );

                CREATE TABLE IF NOT EXISTS candidate_venues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_session_id INTEGER NOT NULL,
                    place_id TEXT NOT NULL,
                    name TEXT,
                    lat REAL NOT NULL,
                    lng REAL NOT NULL,
                    address TEXT,
                    types TEXT,
                    user_ratings_total INTEGER,
                    business_status TEXT,
                    discovery_source TEXT,
                    street_view_score INTEGER DEFAULT 0,
                    panorama_lat REAL,
                    panorama_lng REAL,
                    road_proximity_meters REAL,
                    filter_passed_reasons TEXT,
                    filter_dropped_reason TEXT,
                    is_candidate BOOLEAN DEFAULT 1,
                    FOREIGN KEY (scan_session_id) REFERENCES scan_sessions(id)
                );
                """
            )
            conn.commit()

    @staticmethod
    def compute_cache_hash(query: str, categories: list[str], quantity: int) -> str:
        """Deterministic hash from search parameters."""
        normalized = "|".join(
            [
                query.strip().lower(),
                ",".join(sorted(c.strip().lower() for c in categories)),
                str(quantity),
            ]
        )
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    def get_session(self, cache_hash: str) -> Optional[sqlite3.Row]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM scan_sessions WHERE cache_hash = ? AND expires_at > ?",
                (cache_hash, datetime.utcnow()),
            ).fetchone()
            return row

    def create_session(
        self, cache_hash: str, query: str, categories: list[str], quantity: int
    ) -> int:
        expires = datetime.utcnow() + timedelta(days=7)
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO scan_sessions (cache_hash, query, categories, quantity_target, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_hash) DO UPDATE SET
                    status = 'pending',
                    created_at = CURRENT_TIMESTAMP,
                    expires_at = excluded.expires_at,
                    raw_found = 0,
                    filtered_count = 0
                """,
                (cache_hash, query, ",".join(categories), quantity, expires),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id FROM scan_sessions WHERE cache_hash = ?",
                (cache_hash,),
            ).fetchone()
            return row["id"]

    def store_raw_venues(self, scan_session_id: int, venues: list[dict]) -> None:
        with self._connection() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO raw_venues
                (scan_session_id, place_id, name, lat, lng, address, types,
                 user_ratings_total, business_status, discovery_source, grid_point_lat, grid_point_lng)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        scan_session_id,
                        v["place_id"],
                        v.get("name"),
                        v.get("lat"),
                        v.get("lng"),
                        v.get("address"),
                        json.dumps(v.get("types", [])),
                        v.get("user_ratings_total"),
                        v.get("business_status"),
                        v.get("discovery_source"),
                        v.get("grid_point_lat"),
                        v.get("grid_point_lng"),
                    )
                    for v in venues
                ],
            )
            conn.commit()

    def store_candidates(self, scan_session_id: int, candidates: list[dict]) -> None:
        with self._connection() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO candidate_venues
                (scan_session_id, place_id, name, lat, lng, address, types,
                 user_ratings_total, business_status, discovery_source,
                 street_view_score, panorama_lat, panorama_lng, road_proximity_meters,
                 filter_passed_reasons, filter_dropped_reason, is_candidate)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        scan_session_id,
                        c["place_id"],
                        c.get("name"),
                        c.get("lat"),
                        c.get("lng"),
                        c.get("address"),
                        json.dumps(c.get("types", [])),
                        c.get("user_ratings_total"),
                        c.get("business_status"),
                        c.get("discovery_source"),
                        c.get("street_view_score", 0),
                        c.get("panorama_lat"),
                        c.get("panorama_lng"),
                        c.get("road_proximity_meters"),
                        json.dumps(c.get("filter_passed_reasons", [])),
                        c.get("filter_dropped_reason"),
                        1 if c.get("is_candidate", True) else 0,
                    )
                    for c in candidates
                ],
            )
            conn.commit()

    def update_session_counts(
        self, scan_session_id: int, raw_found: int, filtered_count: int, status: str
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE scan_sessions
                SET raw_found = ?, filtered_count = ?, status = ?
                WHERE id = ?
                """,
                (raw_found, filtered_count, status, scan_session_id),
            )
            conn.commit()

    def get_candidates(self, scan_session_id: int, limit: int = 1500) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM candidate_venues
                WHERE scan_session_id = ? AND is_candidate = 1
                ORDER BY street_view_score DESC, user_ratings_total DESC, name ASC
                LIMIT ?
                """,
                (scan_session_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
