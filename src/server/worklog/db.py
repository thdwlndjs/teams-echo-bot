"""SQLite 연결·스키마·공통 도구. build-log(buildlog/db.py)에서 이 기능에 필요한 부분만 가져왔다."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


class WorklogError(Exception):
    """예상된 실패의 기본 클래스."""


class ValidationError(WorklogError):
    pass


class ConflictError(WorklogError):
    pass


class NotFoundError(WorklogError):
    pass


# 버전 N은 MIGRATIONS[N - 1]. 이미 운영 저장소에 적용된 항목은 고치지 말고 뒤에 추가한다.
MIGRATIONS: list[list[str]] = [
    [
        # 원문 (Teams 메시지, Notion 행). 같은 위치·내용은 한 번만 저장한다.
        """CREATE TABLE sources (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            locator TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            accessible_excerpt TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            metadata_json TEXT,
            parse_status TEXT,
            UNIQUE(kind, locator, content_hash)
        )""",
        # 원문 중 판단 근거가 된 부분(인용).
        """CREATE TABLE evidence (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES sources(id),
            section_locator TEXT NOT NULL,
            excerpt TEXT NOT NULL,
            evidence_kind TEXT NOT NULL
        )""",
        """CREATE TABLE object_evidence (
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL REFERENCES evidence(id),
            PRIMARY KEY (object_type, object_id, evidence_id)
        )""",
        "CREATE INDEX object_evidence_evidence ON object_evidence(evidence_id)",
        # 변경 이력 (누가, 왜, 어떤 상태로).
        """CREATE TABLE revisions (
            id TEXT PRIMARY KEY,
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            snapshot_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            UNIQUE(object_type, object_id, version)
        )""",
        # 분석 실행 (같은 묶음은 한 번만 분석).
        """CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            input_hash TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 1,
            error_kind TEXT,
            error TEXT,
            plan_summary_json TEXT,
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE work_items (
            id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            normalized_title TEXT NOT NULL,
            statement_md TEXT NOT NULL,
            status TEXT NOT NULL,
            attributes_json TEXT NOT NULL,
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        # 대체된 결정은 제목을 그대로 두므로, 살아 있는 항목끼리만 제목이 겹치면 안 된다.
        """CREATE UNIQUE INDEX work_items_live_title ON work_items(scope, kind, normalized_title)
           WHERE status != 'superseded'""",
        """CREATE TABLE work_links (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES work_items(id),
            target_id TEXT NOT NULL REFERENCES work_items(id),
            type TEXT NOT NULL,
            candidate_id TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(source_id, target_id, type)
        )""",
        "CREATE INDEX work_links_target ON work_links(target_id)",
        """CREATE TABLE change_candidates (
            id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL UNIQUE,
            scope TEXT NOT NULL,
            run_id TEXT REFERENCES runs(id),
            change_type TEXT NOT NULL,
            item_kind TEXT NOT NULL,
            target_id TEXT REFERENCES work_items(id),
            target_version INTEGER,
            compared_ids_json TEXT NOT NULL,
            proposal_json TEXT NOT NULL,
            rationale TEXT NOT NULL,
            judged_by TEXT NOT NULL,
            status TEXT NOT NULL,
            decided_by TEXT,
            decided_at TEXT,
            decision_note TEXT,
            applied_item_id TEXT REFERENCES work_items(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX change_candidates_target ON change_candidates(target_id)",
        "CREATE INDEX change_candidates_applied ON change_candidates(applied_item_id)",
        "CREATE INDEX change_candidates_status ON change_candidates(scope, status)",
        """CREATE TABLE work_records (
            source_id TEXT PRIMARY KEY REFERENCES sources(id),
            scope TEXT NOT NULL,
            conversation_key TEXT NOT NULL,
            received_at TEXT NOT NULL,
            token_estimate INTEGER NOT NULL,
            run_id TEXT REFERENCES runs(id)
        )""",
        "CREATE INDEX work_records_pending ON work_records(conversation_key, received_at) WHERE run_id IS NULL",
    ],
    # 2: Notion 구조 지도. 루트 페이지 아래의 섹션(큰 제목 단위)과 역할, 담는 방식, 수집 위치를 기억한다.
    [
        # 분석하지 않고 보관만 하는 이유: reference(원문 보관 섹션), unconfirmed(역할 확인 전), superseded(옛 버전).
        "ALTER TABLE work_records ADD COLUMN hold TEXT",
        # 구조 지문을 비교할 페이지(루트와, 루트 바로 아래 섹션을 품은 하위 페이지).
        """CREATE TABLE source_pages (
            scope TEXT NOT NULL,
            page_id TEXT NOT NULL,
            root_id TEXT NOT NULL,
            path TEXT NOT NULL,
            last_edited TEXT,
            fingerprint TEXT,
            status TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            PRIMARY KEY (scope, page_id)
        )""",
        """CREATE TABLE source_sections (
            id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            page_id TEXT NOT NULL,
            path TEXT NOT NULL,
            title TEXT NOT NULL,
            role TEXT NOT NULL,
            role_source TEXT NOT NULL,
            status TEXT NOT NULL,
            layout_json TEXT NOT NULL,
            cursor TEXT,
            decided_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX source_sections_scope ON source_sections(scope, status)",
        # 하위 페이지·DB 행 단위의 마지막 수정 시각: 안 바뀐 것은 본문을 다시 읽지 않는다.
        """CREATE TABLE source_units (
            id TEXT PRIMARY KEY,
            section_id TEXT NOT NULL REFERENCES source_sections(id),
            last_edited TEXT NOT NULL,
            collected_at TEXT NOT NULL
        )""",
    ],
    # 3: 섹션 역할 판정의 근거(LLM 확신·이유, 물려받음, 추정)를 사람이 확인할 때 볼 수 있게 남긴다.
    [
        "ALTER TABLE source_sections ADD COLUMN role_note TEXT",
    ],
    # 4: 하위 페이지·행이 어느 DB(또는 페이지)에서 왔는지. 한 섹션에 DB가 여러 개 있을 때
    #    (예: 같은 제목 아래 WBS와 멘토링) 회의록 DB의 행만 골라내는 데 쓴다.
    [
        "ALTER TABLE source_units ADD COLUMN parent_id TEXT",
    ],
    # 5: 부분(part). 페이지·DB 행 md를 빈 단락·구분선·제목 사이 블록 묶음으로 나눈 단위와 그 종류.
    #    문서: docs/v1.1/spec.md P1-2, P1-3, "데이터 모델".
    [
        """CREATE TABLE source_parts (
            id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            origin TEXT NOT NULL,            -- notion | teams | clova
            page_id TEXT,                    -- 부분이 나온 페이지·DB 행
            section_id TEXT,                 -- 지도의 섹션
            block_ids_json TEXT,             -- 블록 대조에 성공했을 때 이 부분의 최상위 블록 ID 목록
            parent_part_id TEXT,             -- 종류 판정에서 떼어 낸 하위 토글이면 원래 부분
            path TEXT NOT NULL,
            position INTEGER NOT NULL,
            kind TEXT,                       -- meeting | document | link | NULL(아직 판정 전)
            kind_source TEXT,                -- llm | human
            kind_note TEXT,
            summary TEXT,                    -- document·link의 한 줄 설명
            md TEXT NOT NULL,
            content_hash TEXT NOT NULL,      -- 떼어 내기 전 원래 md의 해시(같으면 다시 판정하지 않는다)
            chars INTEGER NOT NULL,
            state TEXT NOT NULL,             -- collected | extracted | pending_confirm | applied | skipped
            last_edited TEXT,
            confirmed_by TEXT,
            confirmed_at TEXT,
            status TEXT NOT NULL,            -- active | gone
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX source_parts_scope ON source_parts(scope, status, kind)",
        "CREATE INDEX source_parts_page ON source_parts(page_id, position)",
        "CREATE INDEX source_parts_parent ON source_parts(parent_part_id)",
    ],
    # 6: 부분별 추출 결과와 LLM 공급자별 하루 사용량. 문서: docs/v1.1/spec.md P1-4, P1-8.
    #    같은 내용(md 해시)·같은 프롬프트 판이면 다시 추출하지 않는다(재시작해도).
    [
        """CREATE TABLE part_extracts (
            content_hash TEXT NOT NULL,      -- 부분 md(떼어 낸 뒤)의 해시
            prompt_version TEXT NOT NULL,
            model TEXT NOT NULL,
            items_json TEXT NOT NULL,
            dropped INTEGER NOT NULL DEFAULT 0,  -- 원문 인용이 맞지 않아 버린 항목 수
            input_tokens INTEGER,
            output_tokens INTEGER,
            created_at TEXT NOT NULL,
            PRIMARY KEY (content_hash, prompt_version)
        )""",
        # 한도는 공급자(키) 단위라 팀과 상관없이 공급자별 하루 합계를 센다. 백그라운드는 이 합계로 하루 몫을 지킨다.
        """CREATE TABLE llm_usage (
            day TEXT NOT NULL,               -- UTC 날짜
            provider TEXT NOT NULL,          -- groq | cerebras
            tokens INTEGER NOT NULL,
            background_tokens INTEGER NOT NULL,
            PRIMARY KEY (day, provider)
        )""",
    ],
    # 7: 확인 반영(Phase 3). 문서: docs/v1.1/spec.md P1-5, P1-6, "데이터 모델".
    [
        # [고치기] 전후. 사람이 고친 목록이 추출 정답지다.
        """CREATE TABLE extract_corrections (
            id TEXT PRIMARY KEY,
            part_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,      -- 고친 대상 추출의 md 해시
            prompt_version TEXT NOT NULL,
            before_json TEXT NOT NULL,
            after_json TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX extract_corrections_part ON extract_corrections(part_id, created_at)",
        # 반영된 추출의 md 해시. 확인된 회의록이 수정되면 새 추출과 비교해 바뀐 항목만 반영한다.
        "ALTER TABLE source_parts ADD COLUMN applied_hash TEXT",
        # 되돌린(reverted)·취소된(cancelled) 항목은 같은 제목의 새 항목을 막지 않는다.
        "DROP INDEX work_items_live_title",
        """CREATE UNIQUE INDEX work_items_live_title ON work_items(scope, kind, normalized_title)
           WHERE status NOT IN ('superseded', 'reverted', 'cancelled')""",
    ],
    [  # 8: 사람·권한·단계·게이트 (docs/v1.1/spec.md P2, 데이터 모델)
        """CREATE TABLE members (
            teams_user_id TEXT PRIMARY KEY,  -- aad_object_id
            display_name TEXT,               -- Teams 표시 이름(권한 카드에 보임)
            role TEXT,                       -- plan | design | dev | other
            github_login TEXT,
            aliases_json TEXT NOT NULL DEFAULT '[]',
            asked_json TEXT NOT NULL DEFAULT '[]',  -- 처음 등록에서 이미 물은 것(github, aliases)
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE team_members (
            scope TEXT NOT NULL,
            teams_user_id TEXT NOT NULL REFERENCES members(teams_user_id),
            can_approve INTEGER NOT NULL DEFAULT 0,
            is_admin INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (scope, teams_user_id)
        )""",
        """CREATE TABLE team_stages (
            scope TEXT PRIMARY KEY,
            stage TEXT NOT NULL,             -- '1-1' … '3-4'
            approved_by TEXT,
            approved_at TEXT,
            history_json TEXT NOT NULL DEFAULT '[]',
            assessment_json TEXT,            -- 마지막 판정(게이트별 참/거짓·근거, 논의 중 단계)
            assessed_hash TEXT               -- 그때의 반영 항목 지문(같으면 다시 판정하지 않는다)
        )""",
        """CREATE TABLE stage_gates (
            scope TEXT NOT NULL,             -- '*' = 공통 기본값
            stage TEXT NOT NULL,
            gate_id TEXT NOT NULL,
            text TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'open',  -- open | met | later | skipped | removed
            evidence_json TEXT,
            edited_by TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (scope, stage, gate_id)
        )""",
        """CREATE TABLE prompts_asked (
            scope TEXT NOT NULL,
            kind TEXT NOT NULL,              -- stage_advance | gate_missing | skipped_ahead | pivot | delete_confirm | source_change
            key TEXT NOT NULL,
            answer TEXT,
            answered_by TEXT,
            asked_at TEXT NOT NULL,
            answered_at TEXT,
            PRIMARY KEY (scope, kind, key)
        )""",
        "INSERT INTO stage_gates(scope, stage, gate_id, text, state, updated_at) VALUES ('*', '1-1', 'candidates', '후보 주제가 1개 이상이고, 후보마다 해결할 문제가 있다', 'open', '2026-11-04T00:00:00+00:00')",
        "INSERT INTO stage_gates(scope, stage, gate_id, text, state, updated_at) VALUES ('*', '1-2', 'topic_decided', '주제 확정 결정이 있다', 'open', '2026-11-04T00:00:00+00:00')",
        "INSERT INTO stage_gates(scope, stage, gate_id, text, state, updated_at) VALUES ('*', '1-2', 'core_problem', '해결할 핵심 문제가 정해졌다', 'open', '2026-11-04T00:00:00+00:00')",
        "INSERT INTO stage_gates(scope, stage, gate_id, text, state, updated_at) VALUES ('*', '1-3', 'assumptions', '검증할 핵심 가정이 정해졌다', 'open', '2026-11-04T00:00:00+00:00')",
        "INSERT INTO stage_gates(scope, stage, gate_id, text, state, updated_at) VALUES ('*', '1-3', 'target_customer', '타깃 고객이 정해졌다', 'open', '2026-11-04T00:00:00+00:00')",
        "INSERT INTO stage_gates(scope, stage, gate_id, text, state, updated_at) VALUES ('*', '2-1', 'voc_method', '검증 방법과 대상이 정해졌다', 'open', '2026-11-04T00:00:00+00:00')",
        "INSERT INTO stage_gates(scope, stage, gate_id, text, state, updated_at) VALUES ('*', '2-2', 'voc_collected', '수집 결과가 기록됐다', 'open', '2026-11-04T00:00:00+00:00')",
        "INSERT INTO stage_gates(scope, stage, gate_id, text, state, updated_at) VALUES ('*', '2-3', 'voc_result', '가정 검증 결과와 기능 우선순위가 정해졌다', 'open', '2026-11-04T00:00:00+00:00')",
        "INSERT INTO stage_gates(scope, stage, gate_id, text, state, updated_at) VALUES ('*', '3-1', 'backlog', 'GitHub에 백로그 이슈가 있다', 'open', '2026-11-04T00:00:00+00:00')",
        "INSERT INTO stage_gates(scope, stage, gate_id, text, state, updated_at) VALUES ('*', '3-2', 'architecture', '아키텍처 결정이 있다', 'open', '2026-11-04T00:00:00+00:00')",
        "INSERT INTO stage_gates(scope, stage, gate_id, text, state, updated_at) VALUES ('*', '3-3', 'milestones', '마일스톤별 진행이 기록된다', 'open', '2026-11-04T00:00:00+00:00')",
        "INSERT INTO stage_gates(scope, stage, gate_id, text, state, updated_at) VALUES ('*', '3-4', 'release', '릴리스 기준과 충족 여부가 기록됐다', 'open', '2026-11-04T00:00:00+00:00')",
    ],
]
SCHEMA_VERSION = len(MIGRATIONS)


def notion_id(value: str | None) -> str:
    """Notion ID 비교용 표기. 설정에는 하이픈 없이(32자), API 응답에는 하이픈 있게(36자) 들어와서 그대로 비교하면 어긋난다."""
    return (value or "").replace("-", "").lower()


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    text = value if isinstance(value, str) else to_json(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_text(value: str) -> str:
    """비교용 키: NFKC, 대소문자 무시, 공백 하나."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()


def connect(store: Path) -> sqlite3.Connection:
    store.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(store, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


@contextmanager
def write(store: Path) -> Iterator[sqlite3.Connection]:
    """잠금을 잡는 쓰기 트랜잭션 하나: 성공하면 커밋, 어떤 오류든 전부 되돌린다."""
    with closing(connect(store)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def initialize(store: Path) -> dict[str, Any]:
    """저장소를 만들거나 밀린 마이그레이션을 적용한다. 기존 데이터는 지우지 않는다."""
    with write(store) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        current = int(row["value"]) if row is not None else 0
        if current > SCHEMA_VERSION:
            raise ValidationError(f"store schema version {current} is newer than this code ({SCHEMA_VERSION})")
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS[version - 1]:
                connection.execute(statement)
        if current != SCHEMA_VERSION:
            connection.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                               (str(SCHEMA_VERSION),))
    return {"store": str(store), "schema_version": SCHEMA_VERSION}


def add_revision(connection: sqlite3.Connection, object_type: str, object_id: str, version: int,
                 snapshot: dict[str, Any], reason: str, timestamp: str, new_id: str, actor: str) -> None:
    connection.execute(
        """INSERT INTO revisions(id, object_type, object_id, version, snapshot_json, reason, actor, changed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (new_id, object_type, object_id, version, to_json(snapshot), reason, actor, timestamp),
    )
