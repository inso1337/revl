//! The NATIVE half of the analysis: navigation answered in rust, with no
//! interpreter on the path (roadmap item 336, slice 2).
//!
//! Slice 1 computed nothing about a document in rust — every answer came from
//! the embedded reference — because the self-host front end covers only the
//! conformance frontier and a native DIAGNOSTICS engine off that frontier shows
//! green where the reference refuses (design A1, the editor's false-admit).
//!
//! Slice 2 moves the two verbs that do not carry that risk. The design's own
//! statement of why (A4): "for navigation this is the benign direction: a
//! missed symbol yields 'resolve nothing' ... never a WRONG jump to the wrong
//! declaration". So `definition` and the SYMBOL half of `hover` are answered
//! here from `revl_gate::symbols` — the self-host parser's own declaration
//! record — and everything this module is not certain of falls through to the
//! reference:
//!
//! * a document the native front end will not decide (`Symbols::Undecided`);
//! * a name it does not carry, or one a local might shadow;
//! * a hover whose signature it cannot spell exactly as the reference does;
//! * any document that has a DIAGNOSTIC. See [`answerable`] — this one is not
//!   about speed, it is the rule that keeps native navigation from answering
//!   where the reference answers nothing.
//!
//! What did NOT move, and why it cannot: `publishDiagnostics`. See
//! [`extra_diagnostics`].

use serde_json::{json, Value};

use revl_gate::symbols::{Symbol, Symbols};

/// Identifier characters, the same word class `revl.lsp.document` uses to pick
/// the symbol under a cursor.
fn is_word(ch: char) -> bool {
    ch.is_ascii_alphanumeric() || ch == '_'
}

/// The document's declarations as the native front end sees them, or its
/// refusal to decide. Computed once per document version by the server and held
/// beside the text, so a navigation request costs a lookup rather than a parse.
pub fn table_for(text: &str) -> Symbols {
    revl_gate::symbols::symbols(text)
}

/// One zero-based line of the document, `""` past the end. `revl.lsp.document`
/// splits on `\n` and does not add an empty final line for a trailing newline,
/// and `str::split('\n')` matches that exactly.
fn line_text(text: &str, line: i64) -> &str {
    if line < 0 {
        return "";
    }
    text.split('\n').nth(line as usize).unwrap_or("")
}

/// The identifier under a position as `(word, start, end)` in code points, or
/// `None` off an identifier — the rust twin of `document.word_at`, cursor-just-
/// past-the-word tolerance included.
fn word_at(text: &str, line: i64, character: i64) -> Option<(String, usize, usize)> {
    let row: Vec<char> = line_text(text, line).chars().collect();
    if character < 0 || character as usize > row.len() {
        return None;
    }
    let mut col = character as usize;
    if col == row.len() || !is_word(row[col]) {
        if col == 0 || !is_word(row[col - 1]) {
            return None;
        }
        col -= 1;
    }
    let mut start = col;
    while start > 0 && is_word(row[start - 1]) {
        start -= 1;
    }
    let mut end = col;
    while end < row.len() && is_word(row[end]) {
        end += 1;
    }
    Some((row[start..end].iter().collect(), start, end))
}

/// The zero-based column of `name` as a WHOLE word on a one-based source line —
/// the rust twin of `document.find_symbol_column`, which is how the reference
/// turns a line-only declaration into a range.
fn find_symbol_column(text: &str, line: i64, name: &str) -> Option<usize> {
    if line < 1 || name.is_empty() {
        return None;
    }
    let row: Vec<char> = line_text(text, line - 1).chars().collect();
    let needle: Vec<char> = name.chars().collect();
    if needle.len() > row.len() {
        return None;
    }
    for start in 0..=(row.len() - needle.len()) {
        if row[start..start + needle.len()] != needle[..] {
            continue;
        }
        let before = start == 0 || !is_word(row[start - 1]);
        let after = start + needle.len() >= row.len() || !is_word(row[start + needle.len()]);
        if before && after {
            return Some(start);
        }
    }
    None
}

fn span(line: i64, start: usize, end: usize) -> Value {
    json!({
        "start": {"line": line, "character": start},
        "end": {"line": line, "character": end},
    })
}

/// The declaration the native front end resolves the cursor's word to, or
/// `None` for "ask the reference".
fn resolve<'a>(table: &'a Symbols, text: &str, line: i64, character: i64) -> Option<&'a Symbol> {
    let (word, _start, _end) = word_at(text, line, character)?;
    table.get(&word)
}

/// `textDocument/definition` answered natively: the declaration's own line, and
/// the column of its name on that line — the same two numbers, in the same wire
/// shape and key order, that `analysis.compute_definition` produces.
pub fn definition(table: &Symbols, text: &str, uri: &Value, line: i64, character: i64)
    -> Option<Value>
{
    let symbol = resolve(table, text, line, character)?;
    let decl_line = (symbol.line - 1).max(0);
    let column = find_symbol_column(text, symbol.line, &symbol.name).unwrap_or(0);
    Some(json!({"uri": uri, "range": span(decl_line, column, column + symbol.name.chars().count())}))
}

/// Whether the native path may answer navigation for a document at all, given
/// the diagnostics last published for it.
///
/// This is the rule the corpus taught, and it runs the opposite way from the
/// one A4 anticipated. A4 expected the native parser to be LESS capable than
/// the reference ("a symbol the reference parser would resolve is missed"),
/// which is benign. On the real corpus it is sometimes MORE capable: the
/// reference PARSER raises on a large class of refusals (an `effect` with no
/// `undo` raises G4 inside `Parser.parse`), and after a parse failure
/// `analysis.build_symbols` yields an empty table, so the reference resolves
/// NOTHING anywhere in that document. The native parser reads the same document
/// happily and would answer — an extra answer where the reference gives null,
/// which is still a divergence from `python -m revl.lsp`.
///
/// A document with NO diagnostics is exactly the set where the reference's own
/// parse is known to have succeeded, so it is the set the native path may
/// answer over. Everything else goes to the reference. This also subsumes the
/// hover-on-a-diagnostic case: the reference answers those with the guarantee
/// text behind the diagnostic (`diagnostics.explain`), which is reference-side
/// data the native path never has.
pub fn answerable(published: Option<&Value>) -> bool {
    published.and_then(Value::as_array).is_some_and(Vec::is_empty)
}

/// The SYMBOL half of `textDocument/hover`, answered natively.
pub fn hover(table: &Symbols, text: &str, line: i64, character: i64) -> Option<Value> {
    let (word, start, end) = word_at(text, line, character)?;
    let symbol = table.get(&word)?;
    let detail = symbol.detail.as_ref()?;
    Some(json!({
        "contents": {"kind": "markdown", "value": format!("```revl\n{detail}\n```")},
        "range": span(line, start, end),
    }))
}

// ------------------------------------------------------------- diagnostics

/// The revl source tag the reference puts on its own diagnostics.
const REFERENCE_SOURCE: &str = "revl";

/// The tag on a diagnostic the NATIVE gate contributed. A different source
/// makes the two engines' squiggles distinguishable in an editor, and lets the
/// oracle count native contributions without parsing messages.
pub const NATIVE_SOURCE: &str = "revl-native";

/// The self-host gate's tag for "my own parser could not read this". It is a
/// FRONTIER statement wearing a refusal's clothes — it says the self-host front
/// end does not cover the construct (`verified fn`, `pub`, ...), not that the
/// reference would refuse the program — so it is never shown. Measured on the
/// corpus: `examples/rejections/v2_verified_direct_recursion.rvl` draws a `BAD`
/// from the native gate and a real `G7` from the reference, and surfacing the
/// `BAD` would put a second, meaningless squiggle on a correctly-diagnosed
/// document.
const NATIVE_PARSE_FAILURE: &str = "BAD";

/// The native gate's ADDITIONAL squiggles for a document — never a replacement
/// for the reference's.
///
/// The design's slice 2 hoped for an ACCELERATOR here: native `admit` producing
/// the diagnostics for a document the frontier pin proves fully covered, with
/// the reference kept as the off-frontier fallback. The `revl-gate` crate that
/// landed will not support that, and the reason is in its own surface: it
/// "issues no admissions". Its non-refusing arm is `NoObjection`, which means
/// "this gate found nothing it is able to refuse" — it decides the composition
/// and guarantee layer and does NOT run the reference type layer. So no
/// document is ever "proven fully covered": a clean native result cannot show
/// the document is clean, and even a native REFUSAL cannot show the reference
/// would raise only that one diagnostic (a multi-refusal compile carries
/// several). Short-circuiting the reference on either would be the
/// missing-squiggle direction — green in the editor where `revl run` refuses —
/// which the design makes release-blocking.
///
/// What IS sound is the other half of the same rule: "show every diagnostic the
/// reference shows; you may show more, never fewer." A native refusal the
/// reference did not raise is a MORE, so it is added here. On the covered
/// corpus the two byte-agree and this returns nothing, which is what the oracle
/// asserts; if it ever fires, the editor shows an extra squiggle rather than
/// hiding a real one.
pub fn extra_diagnostics(text: &str, reference: &Value) -> Vec<Value> {
    let revl_gate::Verdict::Refused { code, message } = revl_gate::admit(text) else {
        return Vec::new();
    };
    if code == NATIVE_PARSE_FAILURE {
        return Vec::new();
    }
    let already = reference.as_array().is_some_and(|rows| {
        rows.iter()
            .any(|row| row["code"].as_str() == Some(code.as_str())
                 && row["source"].as_str() == Some(REFERENCE_SOURCE))
    });
    if already {
        return Vec::new();
    }
    vec![json!({
        "range": span(0, 0, 0),
        "severity": 1,
        "code": code,
        "source": NATIVE_SOURCE,
        "message": format!(
            "the native revl gate refuses this program and the reference front end did \
             not report it: {message}"
        ),
    })]
}

#[cfg(test)]
mod tests {
    use super::*;

    const SOURCE: &str = "service Counter {\n  fn next() -> Int\n}\n\ncomponent Runner {\n}\n";

    #[test]
    fn a_word_is_picked_up_from_either_edge() {
        assert_eq!(word_at(SOURCE, 0, 8).unwrap().0, "Counter");
        // a cursor just past the last character still names the word
        assert_eq!(word_at(SOURCE, 0, 15).unwrap().0, "Counter");
        // a cursor just past `service` still names it, not the next word
        assert_eq!(word_at(SOURCE, 0, 7).unwrap().0, "service");
        assert!(word_at(SOURCE, 0, 16).is_none()); // the `{`
    }

    #[test]
    fn a_whole_word_column_ignores_a_substring_match() {
        assert_eq!(find_symbol_column("let counters = 1\nCounter", 2, "Counter"), Some(0));
        assert_eq!(find_symbol_column("counters", 1, "Counter"), None);
    }

    #[test]
    fn a_declaration_resolves_to_its_own_line_and_column() {
        let table = table_for(SOURCE);
        let uri = json!("file:///a.rvl");
        let location = definition(&table, SOURCE, &uri, 0, 8).expect("a native location");
        assert_eq!(location["range"]["start"], json!({"line": 0, "character": 8}));
        assert_eq!(location["range"]["end"], json!({"line": 0, "character": 15}));
        assert_eq!(location["uri"], uri);
    }

    #[test]
    fn a_service_hovers_as_the_reference_spells_it() {
        let table = table_for(SOURCE);
        let hover = hover(&table, SOURCE, 0, 8).expect("a native hover");
        assert_eq!(hover["contents"]["value"], "```revl\nservice Counter\n```");
    }

    #[test]
    fn only_a_document_with_no_diagnostics_is_answerable_natively() {
        assert!(answerable(Some(&json!([]))));
        // a diagnostic may mean the reference's own parse failed, after which
        // it resolves nothing anywhere in the document
        assert!(!answerable(Some(&json!([{"code": "G4"}]))));
        assert!(!answerable(None));
        assert!(!answerable(Some(&Value::Null)));
    }

    #[test]
    fn an_undecided_document_resolves_nothing() {
        // `verified fn` is lexed but is not a declaration the self-host front
        // end parses (the front end records a parse problem and the gate fails
        // closed), so the whole document is handed back to the reference. (`pub
        // fn` used to sit here, but the self-host front end now parses `pub`
        // visibility, so it decides such a document rather than declining it.)
        let source = "verified fn f() -> Int {\n  return 1\n}\n";
        let table = table_for(source);
        assert!(table.is_undecided(), "{table:?}");
        assert!(definition(&table, source, &json!("file:///a.rvl"), 0, 12).is_none());
        assert!(hover(&table, source, 0, 12).is_none());
    }

    #[test]
    fn a_native_parse_failure_is_never_shown_as_a_squiggle() {
        // `verified` is not a construct the self-host front end parses, so the
        // native gate answers `BAD` — a frontier gap, not a refusal
        let source = "verified fn recurse(n: Int) -> Int {\n  return recurse(n)\n}\n";
        assert!(extra_diagnostics(source, &json!([])).is_empty());
    }

    #[test]
    fn a_native_refusal_the_reference_already_reported_is_not_added_twice() {
        let source = "component A {\n  provides { k: S }\n}\n";
        let native = extra_diagnostics(source, &json!([]));
        for row in &native {
            assert_eq!(row["source"], NATIVE_SOURCE);
            let code = row["code"].clone();
            let mirrored = json!([{"code": code, "source": "revl"}]);
            assert!(
                extra_diagnostics(source, &mirrored).is_empty(),
                "a reference diagnostic with the same code must suppress the native add"
            );
        }
    }

}
