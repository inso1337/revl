//! JSON serialization that reproduces CPython's `json.dumps` defaults.
//!
//! The exit test for item 336 is byte-identity with `python -m revl.lsp`, and
//! the reference frames its messages with `json.dumps(message)` — which differs
//! from `serde_json::to_vec` in exactly three ways:
//!
//!   * separators are `", "` and `": "`, not `","` and `":"`;
//!   * `ensure_ascii=True`, so every code point outside `0x20..=0x7e` is
//!     written as a `\uXXXX` escape (the hover text carries an em dash, so this
//!     is load-bearing, not theoretical);
//!   * astral code points are escaped as a UTF-16 surrogate pair.
//!
//! Everything else already agrees: the same short escapes (`\n`, `\t`, `\"`,
//! `\\`, ...), the same lowercase hex, the same integer rendering. This module
//! closes the three gaps with a `serde_json::ser::Formatter`, so the binary can
//! emit the reference's bytes rather than a re-ordered equivalent.

use std::io;

use serde::Serialize;
use serde_json::ser::{Formatter, Serializer};
use serde_json::Value;

pub struct PythonFormatter;

impl Formatter for PythonFormatter {
    fn begin_array_value<W>(&mut self, writer: &mut W, first: bool) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        if first {
            Ok(())
        } else {
            writer.write_all(b", ")
        }
    }

    fn begin_object_key<W>(&mut self, writer: &mut W, first: bool) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        if first {
            Ok(())
        } else {
            writer.write_all(b", ")
        }
    }

    fn begin_object_value<W>(&mut self, writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        writer.write_all(b": ")
    }

    /// serde_json hands this the runs of a string it did not escape itself (it
    /// escapes `"`, `\` and the C0 controls). CPython escapes those too, plus
    /// everything above `0x7e`, so the extra escaping happens here.
    fn write_string_fragment<W>(&mut self, writer: &mut W, fragment: &str) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        let mut copied = 0;
        for (index, ch) in fragment.char_indices() {
            let code = ch as u32;
            if (0x20..=0x7e).contains(&code) {
                continue;
            }
            if copied < index {
                writer.write_all(fragment[copied..index].as_bytes())?;
            }
            write_unicode_escape(writer, code)?;
            copied = index + ch.len_utf8();
        }
        if copied < fragment.len() {
            writer.write_all(fragment[copied..].as_bytes())?;
        }
        Ok(())
    }
}

fn write_unicode_escape<W>(writer: &mut W, code: u32) -> io::Result<()>
where
    W: ?Sized + io::Write,
{
    if code < 0x10000 {
        write!(writer, "\\u{:04x}", code)
    } else {
        // the UTF-16 surrogate pair CPython writes for an astral code point
        let value = code - 0x10000;
        write!(
            writer,
            "\\u{:04x}\\u{:04x}",
            0xd800 + (value >> 10),
            0xdc00 + (value & 0x3ff)
        )
    }
}

/// One JSON value as CPython's `json.dumps` would encode it, in UTF-8 bytes.
pub fn dumps(value: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    let mut serializer = Serializer::with_formatter(&mut out, PythonFormatter);
    value
        .serialize(&mut serializer)
        .expect("serializing a serde_json::Value into a Vec cannot fail");
    out
}

/// The compact encoding used for the private worker channel, where nothing is
/// compared byte for byte and one line per message is all that matters.
pub fn dumps_compact(value: &Value) -> Vec<u8> {
    serde_json::to_vec(value).expect("serializing a serde_json::Value cannot fail")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn separators_match_python_defaults() {
        let value = json!({"a": 1, "b": [1, 2]});
        assert_eq!(
            String::from_utf8(dumps(&value)).unwrap(),
            r#"{"a": 1, "b": [1, 2]}"#
        );
    }

    #[test]
    fn non_ascii_is_escaped_like_ensure_ascii() {
        // the em dash the reference puts in hover text
        let value = json!({"v": "G1 \u{2014} declared access"});
        assert_eq!(
            String::from_utf8(dumps(&value)).unwrap(),
            "{\"v\": \"G1 \\u2014 declared access\"}"
        );
    }

    #[test]
    fn astral_code_points_become_surrogate_pairs() {
        let value = json!("\u{1f600}");
        assert_eq!(
            String::from_utf8(dumps(&value)).unwrap(),
            "\"\\ud83d\\ude00\""
        );
    }

    #[test]
    fn control_characters_keep_the_short_escapes() {
        let value = json!("a\nb\tc\u{7f}d");
        assert_eq!(
            String::from_utf8(dumps(&value)).unwrap(),
            "\"a\\nb\\tc\\u007fd\""
        );
    }

    #[test]
    fn object_key_order_is_preserved_from_the_source() {
        let parsed: Value = serde_json::from_str(r#"{"z": 1, "a": 2}"#).unwrap();
        assert_eq!(
            String::from_utf8(dumps(&parsed)).unwrap(),
            r#"{"z": 1, "a": 2}"#
        );
    }
}
