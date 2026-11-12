"""Publish orchestration for the Mímir→vault bridge (stage 2, Task 5).

``publish_entity`` is the only caller that touches the filesystem. Order is
fixed and fail-closed: eligibility → target path → git snapshot → render →
register (first time) → request → replace managed sections → apply → write.
Anything that fails before "write" leaves the vault untouched.

Under the P1 ruling, ``local_only`` facts are refused at the eligibility
gate — the publisher runs, the gate works, admissions stay at zero until
facts become ``external_allowed``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .publication import (
    PublicationService,
    RegisterDocument,
    RequestPublication,
    PublicationPolicyError,
    VAULT_ADAPTER_PRINCIPAL,
    _CARD_ROOT,
    human_content_hash,
    is_publishable_fact,
)
from .vault_card import card_filename, render_card
from .vault_git import GitProtector, VaultGitError


def _snapshot_before_write(vault_root: Path, target: Path) -> dict:
    """Git snapshot before touching the target.

    The vault is target_scope=shared content that leaves the machine via
    Syncthing — every machine write must be recoverable. When the target is
    new there is nothing to protect; the protector records HEAD as-is.
    """
    protector = GitProtector(vault_root)
    try:
        snap = protector.snapshot(f"mimir: before card write {target.name}")
    except VaultGitError as exc:
        raise PublicationPolicyError(f"git snapshot failed: {exc}") from exc
    return {"committed": snap.committed, "commit": snap.commit}


def publish_entity(
    *,
    vault_root: Path,
    store,
    fact: dict,
    entity: dict,
    actor_principal: str,
    sections: dict[str, str],
) -> dict:
    """Publish one entity card into the card namespace. Never touches human text."""
    vault_root = Path(vault_root)
    entity_id = str(entity.get("entity_id") or "").strip()
    if not entity_id:
        return {"status": "skipped", "reason": "missing:entity_id", "path": "", "snapshot": None}
    if actor_principal != VAULT_ADAPTER_PRINCIPAL:
        return {"status": "skipped", "reason": "actor:not_vault_adapter", "path": "", "snapshot": None}

    body = "\n\n".join(str(v) for v in sections.values())
    ok, reason = is_publishable_fact(fact, body, target_scope="shared")
    if not ok:
        return {"status": "skipped", "reason": reason, "path": "", "snapshot": None}

    rel_path = f"{_CARD_ROOT}/{card_filename(entity_id, str(entity.get('name') or ''))}"
    target = vault_root / rel_path
    snapshot = _snapshot_before_write(vault_root, target)

    text = render_card(mimir_id=entity_id, title=str(entity.get("name") or entity_id), sections=sections)

    service = _PublicationSession(store, actor_principal)
    try:
        rendered = service.upsert(vault_path=rel_path, current_text=text, sections=sections)
    except PublicationPolicyError as exc:
        return {"status": "skipped", "reason": str(exc), "path": rel_path, "snapshot": snapshot}

    human_changed = (
        target.exists()
        and human_content_hash(target.read_text(encoding="utf-8")) != human_content_hash(text)
    )
    if human_changed:
        return {"status": "skipped", "reason": "human_content_changed", "path": rel_path, "snapshot": snapshot}

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8", newline="\n")
    # 写后提交机器改动：每次机器写入在 git 里留下可一键回滚的记录
    after = _snapshot_before_write(vault_root, target)
    return {
        "status": "published",
        "reason": "ok",
        "path": rel_path,
        "snapshot": snapshot,
        "commit_after": after["commit"],
    }


class _PublicationSession:
    """Register + request + apply against the store, mirroring on-disk state."""

    def __init__(self, store, actor_principal: str):
        self.service = PublicationService(store)
        self.actor = actor_principal

    def _find_document(self, vault_path: str) -> dict | None:
        with self.service.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE vault_path=?",
                (vault_path,),
            ).fetchone()
        return dict(row) if row else None

    def upsert(self, *, vault_path: str, current_text: str, sections: dict[str, str]) -> str:
        document = self._find_document(vault_path)
        # 幂等键必须区分托管内容版本（卡片正文全托管 → human hash 恒同，
        # 不能当幂等键）；键 = 路径 + 托管内容指纹，内容升级即新键
        managed_fp = hashlib.sha256(
            "\x00".join(f"{k}\x01{v}" for k, v in sorted(sections.items())).encode("utf-8")
        ).hexdigest()[:16]
        idempotency_key = f"card:{vault_path}:{managed_fp}"
        if document is None:
            result = self.service.register_document(
                RegisterDocument(
                    vault_path=vault_path,
                    document_type="overview",
                    owner_principal=self.actor,
                    target_scope="shared",
                ),
                current_text,
            )
            document = self._find_document(vault_path)
            assert document is not None
        else:
            result = {"document_id": document["document_id"]}
        document_id = result["document_id"]
        revision = int(document["current_revision"])
        previous_hash = document["publication_hash"]

        request = self.service.request_publication(
            RequestPublication(
                document_id=document_id,
                expected_revision=revision,
                expected_previous_hash=previous_hash,
                managed_sections=dict(sections),
                idempotency_key=idempotency_key,
            ),
            self.actor,
        )
        applied = self.service.apply_publication(
            request["publication_id"],
            current_text,
            observed_revision=revision,
            observed_publication_hash=previous_hash,
            actor_principal=self.actor,
        )
        return str(applied["rendered_text"])
