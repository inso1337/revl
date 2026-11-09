"""The payee of a payment cannot come out of a mailbox (G9).

`demo/injection/` holds one step written twice. `pay_from_mail.rvl` takes the
account to pay from the text of a mail and is refused; `pay_from_book.rvl`
takes it from a required service, still carries mail text as the memo, and is
admitted. The third test removes the one qualifier the refusal rests on, so a
refusal for any other reason would show up as a failure here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402

DEMO = ROOT / "demo" / "injection"


def test_a_payee_read_from_mail_is_refused():
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(DEMO / "pay_from_mail.rvl")])
    error = excinfo.value
    assert error.code == "G9"
    assert ("untrusted value (mail) flows into the trusted sink `pay` "
            "at argument 1 of `pay`") in str(error)
    # the helper between the read and the payment is named, not skipped
    assert "account_in()" in str(error)


def test_a_payee_from_the_supplier_book_is_admitted_with_a_mail_memo():
    ir = compile_files([str(DEMO / "pay_from_book.rvl")])
    (component,) = ir["components"]
    assert component["name"] == "PayInvoices"
    # mail text does leave with the payment, as the memo, and nothing was
    # declassified to let it
    assert component["taint"] == {"reaches": ["mail"], "declassify": []}


def test_the_refusal_rests_on_the_untrusted_return_alone(tmp_path):
    source = (DEMO / "pay_from_mail.rvl").read_text()
    marked = "fn read_inbox() -> Untrusted[Str]"
    assert source.count(marked) == 1
    unmarked = tmp_path / "pay_from_mail_unmarked.rvl"
    unmarked.write_text(source.replace(marked, "fn read_inbox() -> Str"))
    compile_files([str(unmarked)])
