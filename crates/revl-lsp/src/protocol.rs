//! `Content-Length` framed JSON-RPC 2.0 over a byte stream.
//!
//! The rust twin of `src/revl/lsp/protocol.py`, hand-rolled for the same reason
//! the reference hand-rolls it: the framing is the only part of the server that
//! touches raw bytes, and item 336's exit test is byte-identity with
//! `python -m revl.lsp`, which a protocol crate's own serializer would not give
//! (it re-encodes messages with its own separators and key order). Reading
//! mirrors the reference's tolerances exactly, including returning `None` on a
//! truncated or malformed frame so the loop stops cleanly.

use std::io::{self, BufRead, Write};

use serde_json::{json, Value};

use crate::pyjson;

const CONTENT_LENGTH: &str = "content-length:";

/// Read one framed message, or `None` at EOF / on a malformed frame.
pub fn read_message<R: BufRead>(stream: &mut R) -> Option<Value> {
    let mut length: Option<usize> = None;
    loop {
        let mut line = Vec::new();
        let read = stream.read_until(b'\n', &mut line).ok()?;
        if read == 0 {
            return None;
        }
        if line == b"\r\n" || line == b"\n" {
            break; // end of headers
        }
        let text = String::from_utf8_lossy(&line);
        if text.to_ascii_lowercase().starts_with(CONTENT_LENGTH) {
            length = text
                .split_once(':')
                .and_then(|(_, value)| value.trim().parse::<usize>().ok());
        }
    }
    let length = length?;
    let mut body = vec![0u8; length];
    read_exact_or_none(stream, &mut body)?;
    serde_json::from_slice(&body).ok()
}

fn read_exact_or_none<R: BufRead>(stream: &mut R, buffer: &mut [u8]) -> Option<()> {
    let mut filled = 0;
    while filled < buffer.len() {
        match io::Read::read(stream, &mut buffer[filled..]) {
            Ok(0) => return None, // truncated frame at EOF
            Ok(n) => filled += n,
            Err(ref err) if err.kind() == io::ErrorKind::Interrupted => {}
            Err(_) => return None,
        }
    }
    Some(())
}

/// Serialize and frame one message, in the reference's exact bytes.
pub fn write_message<W: Write>(stream: &mut W, message: &Value) -> io::Result<()> {
    let body = pyjson::dumps(message);
    write!(stream, "Content-Length: {}\r\n\r\n", body.len())?;
    stream.write_all(&body)?;
    stream.flush()
}

pub fn response(request_id: &Value, result: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": request_id, "result": result})
}

pub fn error(request_id: &Value, code: i64, message: &str) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    })
}

pub fn notification(method: &str, params: Value) -> Value {
    json!({"jsonrpc": "2.0", "method": method, "params": params})
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn a_framed_message_round_trips() {
        let message = json!({"jsonrpc": "2.0", "id": 1, "method": "initialize"});
        let mut buffer = Vec::new();
        write_message(&mut buffer, &message).unwrap();
        assert!(buffer.starts_with(b"Content-Length: "));
        let mut cursor = Cursor::new(buffer);
        assert_eq!(read_message(&mut cursor).unwrap(), message);
        assert!(read_message(&mut cursor).is_none()); // clean EOF
    }

    #[test]
    fn a_truncated_body_stops_the_loop() {
        let mut cursor = Cursor::new(b"Content-Length: 40\r\n\r\n{\"a\":1}".to_vec());
        assert!(read_message(&mut cursor).is_none());
    }

    #[test]
    fn the_frame_uses_python_separators() {
        let mut buffer = Vec::new();
        write_message(&mut buffer, &json!({"a": 1, "b": 2})).unwrap();
        let text = String::from_utf8(buffer).unwrap();
        assert!(text.ends_with("{\"a\": 1, \"b\": 2}"), "{text}");
        assert!(text.starts_with("Content-Length: 16\r\n\r\n"), "{text}");
    }
}
