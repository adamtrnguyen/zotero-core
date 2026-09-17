"""One-shot live test of create_annotation against real Zotero.

Highlights "suffers from sim-to-real challenges" in CR-DAgger, red (#ff6666),
with the taxonomy comment. Rects were extracted with PyMuPDF and verified.

⚠ This performs a REAL write to the live Zotero library. The annotation is
deletable in the reader in two clicks; the result prints its key.
"""
from zotero_core.application.services.verbs import create_annotation
from zotero_core.interfaces.factory import build_write_session
import json

ATTACHMENT = "LSF35DSV"  # CR-DAgger "Preprint PDF"

session = build_write_session()
result = create_annotation(
    ATTACHMENT,
    {"pageIndex": 1, "rects": [[405.247, 719.318, 499.366, 729.235]]},
    annotation_type="highlight",
    annotation_text="suffers from sim-to-real challenges",
    annotation_color="#ff6666",
    annotation_comment=(
        "PRIOR: limitation attributed to residual-RL-in-sim approaches. "
        "BORROWED:bare — no citation. "
        "LINK: this bare clause is the premise motivating the paper's design."
    ),
    page_label="2",
    session=session,
)
print(json.dumps(result, indent=2, default=str))
