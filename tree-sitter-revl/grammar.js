/**
 * tree-sitter grammar for revl (syntax-2.0).
 *
 * Mirrors the reference parser in src/revl/parser.py and lexer in
 * src/revl/lexer.py. The grammar defines revl's *syntax*; a handful of the
 * reference parser's rejections are context-sensitive (an LR grammar does not
 * enforce them) and are documented in the README. The corpus honesty check
 * (test/corpus + check.mjs) is the conformance gate.
 */

const PREC = {
  ternary: 1,
  or: 2,
  nullish: 3,
  and: 4,
  bitwise_or: 5,
  bitwise_xor: 6,
  bitwise_and: 7,
  equality: 8,
  comparison: 9,
  shift: 10,
  additive: 11,
  multiplicative: 12,
  unary: 13,
  postfix: 14,
};

const commaSep = (rule) => optional(commaSep1(rule));
const commaSep1 = (rule) => seq(rule, repeat(seq(',', rule)), optional(','));
const commaSep1NoTrail = (rule) => seq(rule, repeat(seq(',', rule)));

module.exports = grammar({
  name: 'revl',

  externals: ($) => [$.template_string, $._host_body],

  // `;` is an optional statement separator/terminator (item 157). The
  // reference lexer skips it between statements (`_skip_semis`), so it carries
  // no syntax of its own; treating it as an extra mirrors that exactly.
  extras: ($) => [/\s/, ';', $.comment],

  conflicts: ($) => [
    // `{ … }` heads a block, a record literal and a functional record update;
    // one lookahead token cannot tell them apart.
    [$.record_literal, $.block],
    [$.record_literal, $.effect_block],
    // A value / type / arrow-parameter that begins with `IDENT`, `IDENT[…]` or
    // `( … )` is ambiguous until more of it is seen (revl has no statement
    // terminator token — `;`/newlines are extras — so a type in a trailing
    // slot like `handoff k: T` can abut the next statement).
    [$.parameter, $._expression],
    [$._expression, $.generic_type],
    [$._expression, $.type_identifier],
    [$.type_identifier, $.generic_type],
    [$.parameter, $.type_identifier],
    [$.parameter, $._expression, $.type_identifier],
  ],

  rules: {
    source_file: ($) => repeat($._declaration),

    comment: ($) => token(seq('//', /.*/)),

    _declaration: ($) =>
      choice(
        $.use_declaration,
        $.service_declaration,
        $.component_declaration,
        $.type_declaration,
        $.function_declaration,
        $.extern_declaration,
        $.test_declaration,
        $.fault_test_declaration,
        $.prop_test_declaration,
      ),

    // ---------------------------------------------------------------- use

    use_declaration: ($) =>
      seq(
        'use',
        field('path', $.string),
        choice(
          field('names', $.import_names),
          seq('as', field('alias', $.identifier)),
        ),
      ),

    import_names: ($) => seq('{', commaSep($.identifier), '}'),

    // ------------------------------------------------------------ service

    service_declaration: ($) =>
      seq(
        optional('pub'),
        optional('commutative'),
        'service',
        field('name', $.identifier),
        field('body', $.service_body),
      ),

    service_body: ($) => seq('{', repeat($.method_signature), '}'),

    method_signature: ($) =>
      seq(
        repeat($.method_modifier),
        'fn',
        field('name', $.identifier),
        field('parameters', $.typed_parameters),
        optional(seq('->', field('return_type', $._type))),
      ),

    method_modifier: ($) =>
      choice(
        seq('emission', optional($.capability_list)),
        'async',
        'commutative',
      ),

    // A capability set: `[net]`, `[net(requests=100)]`, `[fs.write(path="/tmp")]`,
    // `[fs, shell]`. Each capability is a dotted name with an optional
    // parameter list of `key = <literal>` (or bare) arguments (items 260/296).
    capability_list: ($) => seq('[', commaSep1NoTrail($.capability), ']'),

    capability: ($) =>
      seq(
        field('name', $.capability_name),
        optional(field('parameters', $.capability_parameters)),
      ),

    capability_name: ($) =>
      seq($.identifier, repeat(seq('.', $.identifier))),

    capability_parameters: ($) => seq('(', commaSep($.capability_parameter), ')'),

    capability_parameter: ($) =>
      seq(
        field('name', $.identifier),
        optional(seq('=', field('value', $._literal))),
      ),

    typed_parameters: ($) => seq('(', commaSep($.typed_parameter), ')'),

    typed_parameter: ($) =>
      seq(field('name', $.identifier), ':', field('type', $._type)),

    // ---------------------------------------------------------- component

    component_declaration: ($) =>
      seq(
        optional('boot'),
        'component',
        field('name', $.identifier),
        repeat(choice($.requires_clause, $.provides_clause)),
        field('body', $.component_body),
      ),

    requires_clause: ($) => seq('requires', commaSep1NoTrail($.binding)),
    provides_clause: ($) => seq('provides', commaSep1NoTrail($.binding)),

    binding: ($) =>
      seq(field('local', $.identifier), ':', field('service', $.identifier)),

    component_body: ($) =>
      seq('{', repeat(choice($.config_block, $._statement)), '}'),

    config_block: ($) => seq('config', '{', commaSep($.config_field), '}'),

    config_field: ($) =>
      seq(
        field('name', $.identifier),
        ':',
        field('type', $._type),
        optional(field('bound', $.config_bound)),
        optional(seq('=', field('default', $._literal))),
      ),

    // The optional admission bound on a `boot component` config field
    // (item 350): `under "<prefix>"` (a confined path) or `in [lit, ...]`
    // (an enumerated set). The reference restricts it to boot components at
    // parse time — a context-sensitive rule not expressible here.
    config_bound: ($) =>
      choice(
        seq('under', field('prefix', $.string)),
        seq('in', field('values', $.list_literal)),
      ),

    // --------------------------------------------------------------- type

    type_declaration: ($) =>
      seq(
        optional('pub'),
        'type',
        field('name', $.identifier),
        optional($.type_parameters),
        '=',
        field('value', choice($.record_type, $.variant_type, $.type_alias)),
      ),

    type_parameters: ($) => seq('[', commaSep1NoTrail($.identifier), ']'),

    record_type: ($) => seq('{', commaSep($.record_type_field), '}'),

    record_type_field: ($) =>
      seq(field('name', $.identifier), ':', field('type', $._type)),

    variant_type: ($) => seq($.variant_case, repeat1(seq('|', $.variant_case))),

    variant_case: ($) =>
      seq(
        field('name', $.identifier),
        optional(seq('(', field('payload', $._type), ')')),
      ),

    type_alias: ($) => $._type,

    // ------------------------------------------------------------- extern

    extern_declaration: ($) =>
      seq(
        optional('pub'),
        'extern',
        repeat($.extern_modifier),
        'fn',
        field('name', $.identifier),
        optional($.type_parameters),
        field('parameters', $.typed_parameters),
        optional(seq('->', field('return_type', $._type))),
        optional(seq('undo', field('undo', $._expression))),
        optional(seq('compensate', field('compensate', $._expression))),
        repeat1(seq('=', $.host_block)),
      ),

    // An extern's classification/colour prelude: `pure` / `acquire` /
    // `emission[caps]?` plus an optional `async` colour, in any order
    // (`extern emission async fn …`, `extern pure fn …`).
    extern_modifier: ($) =>
      choice(
        'pure',
        'acquire',
        'async',
        seq('emission', optional($.capability_list)),
      ),

    host_block: ($) =>
      seq('@', field('backend', $.identifier), alias($._host_body, $.host_body)),

    // ----------------------------------------------------------- function

    function_declaration: ($) =>
      seq(
        optional('pub'),
        optional('verified'),
        'fn',
        field('name', $.identifier),
        optional($.type_parameters),
        field('parameters', $.typed_parameters),
        optional(seq('->', field('return_type', $._type))),
        field('body', $.block),
      ),

    // -------------------------------------------------------------- tests

    test_declaration: ($) =>
      seq(
        optional('lifecycle'),
        'test',
        field('name', $.string),
        field('body', $.test_body),
      ),

    // A test body accepts both pure statements and the lifecycle-test
    // statements (load / unload / call / swap). The reference restricts the
    // latter to `lifecycle test` bodies; that is a context-sensitive rule.
    test_body: ($) =>
      seq('{', repeat(choice($._lifecycle_statement, $._statement)), '}'),

    // `prop test "name" (p: T, …) { assert … }` — property test with
    // type-derived generated inputs (item 37). The parameter list is a `fn`
    // parameter list; the body is the pure statement grammar a plain test uses.
    prop_test_declaration: ($) =>
      seq(
        'prop',
        'test',
        field('name', $.string),
        field('parameters', $.typed_parameters),
        field('body', $.block),
      ),

    fault_test_declaration: ($) =>
      seq(
        'fault',
        'test',
        field('name', $.string),
        'for',
        field('component', $.identifier),
        optional($.fault_config),
        '{',
        repeat(choice($.fault_injection, $.fault_assertion)),
        '}',
      ),

    fault_config: ($) => seq('with', '{', commaSep($.fault_config_field), '}'),
    fault_config_field: ($) =>
      seq(field('name', $.identifier), ':', field('value', $._literal)),

    fault_injection: ($) =>
      seq(
        'fail',
        'at',
        choice(
          seq('step', $.integer),
          seq('effect', choice($.identifier, $.string)),
        ),
      ),

    fault_assertion: ($) =>
      seq(
        'assert',
        choice(
          'failed',
          seq('no', choice('residue', 'emissions')),
          seq('inverses', 'lifo'),
          seq('siblings', 'unaffected'),
        ),
      ),

    // --------------------------------------------------------- statements

    block: ($) => seq('{', repeat($._statement), '}'),

    _statement: ($) =>
      choice(
        $.let_statement,
        $.bound_call,
        $.assignment_statement,
        $.return_statement,
        $.if_statement,
        $.while_statement,
        $.for_statement,
        $.assert_statement,
        $.effect_statement,
        $.emit_statement,
        $.fail_statement,
        $.await_statement,
        $.isolate_statement,
        $.intercept_statement,
        $.provide_statement,
        $.handoff_statement,
        $.timer_statement,
        $.expression_statement,
      ),

    _lifecycle_statement: ($) =>
      choice(
        $.load_statement,
        $.unload_statement,
        $.call_statement,
        $.advance_statement,
      ),

    // `handoff <key>: <Type>` — the verified state hand-off prelude of a
    // stateful component (item 53). A component prelude declaration, refused
    // inside a method body by the reference (a context-sensitive rule).
    handoff_statement: ($) =>
      seq('handoff', field('key', $.identifier), ':', field('type', $._type)),

    // `every <dur> { emit … }` (periodic) / `after <dur> { emit … }` (one-shot).
    // The delay is `<int><unit>` (`30s`, `5m`, `250ms`); the body is emissions.
    // (item 57, docs/time-coeffect.md)
    timer_statement: ($) =>
      seq(
        field('mode', choice('every', 'after')),
        field('interval', $.duration),
        field('body', $.timer_body),
      ),

    timer_body: ($) => seq('{', repeat($.emit_statement), '}'),

    // A duration literal — the reference lexer has no duration token, so `30s`
    // is an integer immediately followed by a bare unit ident (ms|s|m|h|d).
    duration: ($) =>
      seq(field('count', $.integer), field('unit', alias($.identifier, $.time_unit))),

    // `advance <dur>` — drive the deterministic test clock forward, the one
    // lifecycle statement that takes a duration (item 102).
    advance_statement: ($) => seq('advance', field('interval', $.duration)),

    let_statement: ($) =>
      seq(
        choice('let', 'var'),
        field('binding', choice($.identifier, $.record_pattern, $.list_pattern)),
        optional(seq(':', field('type', $._type))),
        '=',
        field('value', choice($.effect_expression, $.emit_expression, $._expression)),
      ),

    record_pattern: ($) => seq('{', commaSep1NoTrail($.identifier), '}'),

    list_pattern: ($) =>
      seq(
        '[',
        commaSep1NoTrail($.identifier),
        optional(seq(',', '...', $.identifier)),
        ']',
      ),

    assignment_statement: ($) =>
      seq(
        field('left', $.identifier),
        field('operator', choice('=', '+=', '-=', '*=', '/=', '%=')),
        field('right', $._expression),
      ),

    return_statement: ($) => prec.right(seq('return', optional($._expression))),

    if_statement: ($) =>
      prec.right(
        seq(
          'if',
          '(',
          field('condition', $._expression),
          ')',
          field('consequence', choice($.block, $._statement)),
          optional(seq('else', field('alternative', choice($.block, $._statement)))),
        ),
      ),

    while_statement: ($) =>
      seq('while', '(', field('condition', $._expression), ')', field('body', choice($.block, $._statement))),

    for_statement: ($) =>
      seq(
        'for',
        '(',
        field('binding', $.identifier),
        'of',
        field('iterable', $._expression),
        ')',
        field('body', choice($.block, $._statement)),
      ),

    assert_statement: ($) => seq('assert', $._expression),

    effect_statement: ($) => $.effect_expression,

    effect_expression: ($) =>
      seq(
        'effect',
        optional('await'),
        choice($.spawn_expression, $.effect_block, $._expression),
        optional(seq('undo', field('undo', $._expression))),
      ),

    effect_block: ($) => seq('{', repeat($._statement), '}'),

    spawn_expression: ($) =>
      seq('spawn', field('component', $.identifier), optional(seq('with', $.record_literal))),

    // At statement position `emit call()` is preferred over an expression
    // statement wrapping a unary `emit` (only emit_statement carries the
    // `compensate` clause); the higher precedence breaks the tie.
    emit_statement: ($) =>
      prec.right(
        seq('emit', $._expression, optional(seq('compensate', field('compensate', $._expression)))),
      ),

    fail_statement: ($) => seq('fail', $._expression),

    await_statement: ($) => seq('await', $._expression),

    isolate_statement: ($) =>
      seq('isolate', field('key', $.identifier), 'in', $.realm_expression),

    realm_expression: ($) => seq('realm', '(', $._expression, ')'),

    intercept_statement: ($) =>
      seq('intercept', field('key', $.identifier), 'with', $.record_literal),

    provide_statement: ($) =>
      seq('provide', field('key', $.identifier), '{', repeat($.provide_method), '}'),

    provide_method: ($) =>
      seq(
        optional('async'),
        'fn',
        field('name', $.identifier),
        field('parameters', $.parameters),
        optional(seq('->', field('return_type', $._type))),
        choice(
          seq('=', field('body', choice($.emit_expression, $._expression))),
          field('body', $.block),
        ),
      ),

    // provide-method / arrow parameters: names with OPTIONAL type annotations
    parameters: ($) => seq('(', commaSep($.parameter), ')'),
    parameter: ($) =>
      seq(field('name', $.identifier), optional(seq(':', field('type', $._type)))),

    // lifecycle-test statements
    load_statement: ($) =>
      seq('load', field('component', $.identifier), optional(seq('with', $.config_arguments))),

    config_arguments: ($) => seq('{', commaSep($.config_argument), '}'),
    config_argument: ($) =>
      seq(field('name', $.identifier), ':', field('value', $._expression)),

    unload_statement: ($) => seq('unload', field('component', $.identifier)),

    call_statement: ($) =>
      seq(
        'call',
        field('key', $.identifier),
        '.',
        field('method', $.identifier),
        field('arguments', $.arguments),
      ),

    bound_call: ($) =>
      seq(choice('let', 'var'), field('binding', $.identifier), '=', $.call_statement),

    expression_statement: ($) => $._expression,

    // --------------------------------------------------------- expressions

    _expression: ($) =>
      choice(
        $.identifier,
        $.config,
        $.integer,
        $.float,
        $.string,
        $.template_string,
        $.boolean,
        $.null,
        $.hole,
        $.record_literal,
        $.record_update,
        $.list_literal,
        $.match_expression,
        $.arrow_function,
        $.parenthesized_expression,
        $.unary_expression,
        $.binary_expression,
        $.ternary_expression,
        $.call_expression,
        $.member_expression,
        $.optional_member_expression,
        $.index_expression,
      ),

    config: ($) => prec(-1, 'config'),

    parenthesized_expression: ($) => seq('(', $._expression, ')'),

    unary_expression: ($) =>
      prec.right(
        PREC.unary,
        seq(field('operator', choice('!', '-', '~')), field('operand', $._expression)),
      ),

    // `emit <call>` in value position (parser.py EmitExpr): the value of an
    // irreversible call, e.g. `fn record(line) = emit append_line(...)`. Kept
    // out of the general expression set so `emit` at statement position is
    // unambiguously an emit_statement.
    emit_expression: ($) =>
      prec.right(seq('emit', field('operand', $._expression))),

    binary_expression: ($) => {
      const table = [
        ['||', PREC.or],
        ['??', PREC.nullish],
        ['&&', PREC.and],
        ['|', PREC.bitwise_or],
        ['^', PREC.bitwise_xor],
        ['&', PREC.bitwise_and],
        ['==', PREC.equality],
        ['===', PREC.equality],
        ['!=', PREC.equality],
        ['!==', PREC.equality],
        ['<', PREC.comparison],
        ['>', PREC.comparison],
        ['<=', PREC.comparison],
        ['>=', PREC.comparison],
        ['<<', PREC.shift],
        ['>>', PREC.shift],
        ['+', PREC.additive],
        ['-', PREC.additive],
        ['*', PREC.multiplicative],
        ['/', PREC.multiplicative],
        ['%', PREC.multiplicative],
      ];
      return choice(
        ...table.map(([op, p]) => {
          const assoc = op === '??' ? prec.right : prec.left;
          return assoc(
            p,
            seq(
              field('left', $._expression),
              field('operator', op),
              field('right', $._expression),
            ),
          );
        }),
      );
    },

    ternary_expression: ($) =>
      prec.right(
        PREC.ternary,
        seq(
          field('condition', $._expression),
          '?',
          field('consequence', choice($.emit_expression, $._expression)),
          ':',
          field('alternative', choice($.emit_expression, $._expression)),
        ),
      ),

    call_expression: ($) =>
      prec(PREC.postfix, seq(field('function', $._expression), field('arguments', $.arguments))),

    arguments: ($) => seq('(', commaSep($._expression), ')'),

    member_expression: ($) =>
      prec(PREC.postfix, seq(field('object', $._expression), '.', field('property', $.identifier))),

    optional_member_expression: ($) =>
      prec(PREC.postfix, seq(field('object', $._expression), '?.', field('property', $.identifier))),

    index_expression: ($) =>
      prec(PREC.postfix, seq(field('object', $._expression), '[', field('index', $._expression), ']')),

    arrow_function: ($) =>
      prec.right(
        -1,
        seq(
          field('parameters', choice($.identifier, $.parameters)),
          optional(seq(':', field('return_type', $._type))),
          '=>',
          field('body', choice($.block, $.emit_expression, $._expression)),
        ),
      ),

    record_literal: ($) => seq('{', commaSep($.record_entry), '}'),
    record_entry: ($) =>
      seq(field('name', $.identifier), ':', field('value', $._expression)),

    // Functional record update: `{ base | field = expr, … }` (item 382). The
    // base is any expression; each entry replaces one field by name.
    record_update: ($) =>
      seq(
        '{',
        field('base', $._expression),
        '|',
        commaSep1($.record_update_entry),
        '}',
      ),

    record_update_entry: ($) =>
      seq(field('name', $.identifier), '=', field('value', $._expression)),

    list_literal: ($) => seq('[', commaSep($._expression), ']'),

    match_expression: ($) =>
      seq('match', field('value', $._expression), '{', commaSep($.match_arm), '}'),

    match_arm: ($) =>
      seq(field('pattern', $.match_pattern), '=>', field('value', $._expression)),

    match_pattern: ($) =>
      seq(field('case', $.identifier), optional(seq('(', field('binding', $.identifier), ')'))),

    hole: ($) =>
      prec.right(
        seq('hole', optional(seq('[', field('type', $._type), ']')), optional(field('message', $.string))),
      ),

    // --------------------------------------------------------------- types

    _type: ($) => choice($._type_no_opt, $.optional_type),

    optional_type: ($) => prec(1, seq($._type_no_opt, '?')),

    _type_no_opt: ($) =>
      choice(
        $.function_type,
        $.generic_type,
        $.parenthesized_type,
        $.type_identifier,
      ),

    type_identifier: ($) => $.identifier,

    generic_type: ($) =>
      seq(field('name', $.identifier), '[', commaSep1NoTrail($._type), ']'),

    function_type: ($) =>
      prec.right(seq('(', commaSep($._type), ')', '->', field('return', $._type))),

    parenthesized_type: ($) => seq('(', $._type, ')'),

    // -------------------------------------------------------------- tokens

    _literal: ($) =>
      choice($.integer, $.float, $.string, $.boolean, $.null, $.negative_number),

    negative_number: ($) => seq('-', choice($.integer, $.float)),

    boolean: ($) => choice('true', 'false'),
    null: ($) => 'null',

    // Integer literals with the `0x` / `0b` / `0o` radices and `_` digit-group
    // separators (item 381). The reference canonicalizes them numerically.
    integer: ($) =>
      token(
        choice(
          /0[xX][0-9a-fA-F][0-9a-fA-F_]*/,
          /0[bB][01][01_]*/,
          /0[oO][0-7][0-7_]*/,
          /\d[\d_]*/,
        ),
      ),
    float: ($) =>
      token(choice(/\d[\d_]*\.\d[\d_]*([eE][+-]?\d+)?/, /\d[\d_]*[eE][+-]?\d+/)),

    // String literals in all three spellings — `"…"`, `'…'` and triple-quoted
    // `"""…"""` — with `\"` / `\'` / `\\` (and other `\x`) escapes (items
    // 182/382). The reference lexer treats a `\`-escape as one unit, so an
    // escaped quote never closes the string.
    string: ($) =>
      token(
        choice(
          seq('"""', repeat(choice(/[^"\\]/, /"[^"]/, /""[^"]/, /\\./)), '"""'),
          seq('"', repeat(choice(/[^"\\\n]/, /\\./)), '"'),
          seq("'", repeat(choice(/[^'\\\n]/, /\\./)), "'"),
        ),
      ),

    identifier: ($) => /[A-Za-z_][A-Za-z0-9_]*/,
  },
});
