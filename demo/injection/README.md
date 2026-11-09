# The injection demo: mail cannot choose the payee

One step, written twice. The task is "pay this week's supplier invoices", and
one mail in the inbox says the supplier's bank details changed.

| file | where the payee comes from | verdict |
|---|---|---|
| [`pay_from_mail.rvl`](pay_from_mail.rvl) | the text of the mail, through a helper | refused, G9 |
| [`pay_from_book.rvl`](pay_from_book.rvl) | `suppliers`, a service the component requires | admitted |

```sh
revl compile demo/injection/pay_from_mail.rvl   # exit 1
revl compile demo/injection/pay_from_book.rvl   # exit 0
```

The refusal begins, after its file and line prefix:

```
untrusted value (mail) flows into the trusted sink `pay` at argument 1 of `pay`
```

Its hint ends with the path the value took: `read_inbox() -> account_in() -> pay`.

## What it shows

- The refusal rests on two declarations and nothing else. `read_inbox` returns
  `Untrusted[Str]` and `pay` takes its payee as `Trusted[Str]`. No rule
  mentions mail, banks or the wording of a message.
- It is a verdict on the shape of the step. No mail is read to reach it, so it
  does not depend on a model noticing that a message is hostile.
- Mail text is still usable as data. The admitted version sends the first line
  of the mail as the payment memo. Only the payee position is authority.
- Paying is an emission in both versions. A host that pends emissions still
  asks a human before the money moves.

## What it does not show

- A step that pays the right supplier the wrong amount is in bounds: `cents`
  is not a trusted position here. G9 caps where authority may come from, not
  what a granted capability is used for
  ([docs/prompt-injection-resistance.md](../../docs/prompt-injection-resistance.md),
  residual risks).
- The book decides the account for a name, not which name gets paid.
  `settle` takes the supplier as a plain `Str`, so a caller steered into
  naming another supplier in the book is in bounds too.
- Nothing runs. There is no agent, no model and no mailbox in this directory,
  only the two sources the gate rules on.

[`demo/escape`](../escape/README.md) has the same guarantee on a shell sink,
with the refusal, the repair and the audit of the repaired source driven end
to end. `tests/test_injection_demo_payee.py` pins both verdicts here, and that
the refusal goes away when the one `Untrusted` qualifier is removed.
