#include "tree_sitter/parser.h"

// External scanner for two revl tokens whose lexing is not regular:
//
//   * TEMPLATE_STRING — a backtick template `...${expr}...`. The `${ ... }`
//     interpolations are brace-balanced (they may hold record literals), and
//     the template ends at the first backtick that is not inside an
//     interpolation. This mirrors src/revl/lexer.py:_lex_template, which scans
//     char by char and balances only braces inside `${`. Captured as one
//     opaque token; highlighting paints the whole literal as a string.
//
//   * HOST_BODY — the `{ ...verbatim... }` of an `@backend { ... }` host block
//     (extern bodies). The body is host-language text, not revl, so it is
//     consumed as a brace-balanced run, exactly as the reference lexer does.

enum TokenType {
  TEMPLATE_STRING,
  HOST_BODY,
};

void *tree_sitter_revl_external_scanner_create(void) { return NULL; }
void tree_sitter_revl_external_scanner_destroy(void *p) { (void)p; }
unsigned tree_sitter_revl_external_scanner_serialize(void *p, char *b) {
  (void)p; (void)b; return 0;
}
void tree_sitter_revl_external_scanner_deserialize(void *p, const char *b, unsigned n) {
  (void)p; (void)b; (void)n;
}

static void advance(TSLexer *lexer) { lexer->advance(lexer, false); }
static void skip(TSLexer *lexer) { lexer->advance(lexer, true); }

static bool scan_template(TSLexer *lexer) {
  while (lexer->lookahead == ' ' || lexer->lookahead == '\t' ||
         lexer->lookahead == '\n' || lexer->lookahead == '\r') {
    skip(lexer);
  }
  if (lexer->lookahead != '`') return false;
  advance(lexer);
  while (lexer->lookahead != 0) {
    if (lexer->lookahead == '`') {
      advance(lexer);                       // closing backtick
      lexer->result_symbol = TEMPLATE_STRING;
      lexer->mark_end(lexer);
      return true;
    }
    if (lexer->lookahead == '$') {
      advance(lexer);
      if (lexer->lookahead == '{') {
        // consume a brace-balanced interpolation body
        advance(lexer);
        int depth = 1;
        while (lexer->lookahead != 0 && depth > 0) {
          if (lexer->lookahead == '{') depth++;
          else if (lexer->lookahead == '}') depth--;
          advance(lexer);
        }
        if (depth != 0) return false;       // unterminated ${
      }
      continue;
    }
    advance(lexer);
  }
  return false;                             // unterminated template
}

static bool scan_host_body(TSLexer *lexer) {
  while (lexer->lookahead == ' ' || lexer->lookahead == '\t' ||
         lexer->lookahead == '\n' || lexer->lookahead == '\r') {
    skip(lexer);
  }
  if (lexer->lookahead != '{') return false;
  advance(lexer);
  int depth = 1;
  while (lexer->lookahead != 0 && depth > 0) {
    if (lexer->lookahead == '{') depth++;
    else if (lexer->lookahead == '}') depth--;
    advance(lexer);
  }
  if (depth != 0) return false;
  lexer->result_symbol = HOST_BODY;
  lexer->mark_end(lexer);
  return true;
}

bool tree_sitter_revl_external_scanner_scan(void *payload, TSLexer *lexer,
                                            const bool *valid_symbols) {
  (void)payload;
  if (valid_symbols[TEMPLATE_STRING] && scan_template(lexer)) {
    return true;
  }
  if (valid_symbols[HOST_BODY]) {
    return scan_host_body(lexer);
  }
  return false;
}
