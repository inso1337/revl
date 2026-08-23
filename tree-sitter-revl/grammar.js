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
  equality: 5,
  comparison: 6,
  additive: 7,
  multiplicative: 8,
  unary: 9,
  postfix: 10,
};

const commaSep = (rule) => optional(commaSep1(rule));
const commaSep1 = (rule) => seq(rule, repeat(seq(',', rule)), optional(','));
const commaSep1NoTrail = (rule) => seq(rule, repeat(seq(',', rule)));

module.exports = grammar({
  name: 'revl',

  externals: ($) => [$.template_string, $._host_body],

  extras: ($) => [/\s/, $.comment],

  conflicts: ($) => [
    [$.record_literal, $.block],
    [$.record_literal, $.effect_block],
    [$.parameter, $._expression],
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

    capability_list: ($) => seq('[', commaSep1NoTrail($.identifier), ']'),

    typed_parameters: ($) => seq('(', commaSep($.typed_parameter), ')'),

    typed_parameter: ($) =>
      seq(field('name', $.identifier), ':', field('type', $._type)),

    // ---------------------------------------------------------- component

    component_declaration: ($) =>
      seq(
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
        optional(seq('=', field('default', $._literal))),
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
        optional(field('classification', choice('pure', 'acquire', 'emission'))),
        'fn',
        field('name', $.identifier),
        optional($.type_parameters),
        field('parameters', $.typed_parameters),
        optional(seq('->', field('return_type', $._type))),
        optional(seq('undo', field('undo', $._expression))),
        optional(seq('compensate', field('compensate', $._expression))),
        repeat1(seq('=', $.host_block)),
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
        $.expression_statement,
      ),

    _lifecycle_statement: ($) =>
      choice($.load_statement, $.unload_statement, $.call_statement, $.swap_statement),

    let_statement: ($) =>
      seq(
        choice('let', 'var'),
        field('binding', choice($.identifier, $.record_pattern, $.list_pattern)),
        optional(seq(':', field('type', $._type))),
        '=',
        field('value', choice($.effect_expression, $._expression)),
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
        choice($.spawn_expression, $.effect_block, $._expression),
        optional(seq('undo', field('undo', $._expression))),
      ),

    effect_block: ($) => seq('{', repeat($._statement), '}'),

    spawn_expression: ($) =>
      seq('spawn', field('component', $.identifier), optional(seq('with', $.record_literal))),

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
        choice(seq('=', field('body', $._expression)), field('body', $.block)),
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

    swap_statement: ($) =>
      seq('swap', field('from', $.identifier), '->', field('to', $.identifier)),

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
        seq(field('operator', choice('!', '-')), field('operand', $._expression)),
      ),

    binary_expression: ($) => {
      const table = [
        ['||', PREC.or],
        ['??', PREC.nullish],
        ['&&', PREC.and],
        ['==', PREC.equality],
        ['===', PREC.equality],
        ['!=', PREC.equality],
        ['!==', PREC.equality],
        ['<', PREC.comparison],
        ['>', PREC.comparison],
        ['<=', PREC.comparison],
        ['>=', PREC.comparison],
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
          field('consequence', $._expression),
          ':',
          field('alternative', $._expression),
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
          '=>',
          field('body', $._expression),
        ),
      ),

    record_literal: ($) => seq('{', commaSep($.record_entry), '}'),
    record_entry: ($) =>
      seq(field('name', $.identifier), ':', field('value', $._expression)),

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

    integer: ($) => /\d+/,
    float: ($) => token(choice(/\d+\.\d+([eE][+-]?\d+)?/, /\d+[eE][+-]?\d+/)),

    string: ($) => token(seq('"', /[^"\n]*/, '"')),

    identifier: ($) => /[A-Za-z_][A-Za-z0-9_]*/,
  },
});
