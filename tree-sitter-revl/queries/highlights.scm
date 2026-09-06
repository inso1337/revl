; Syntax highlighting for revl (syntax-2.0).
; Keyword sets mirror src/revl/lexer.py KEYWORDS.

; ---------------------------------------------------------------- comments
(comment) @comment

; ------------------------------------------------------------------ literals
(string) @string
(template_string) @string
(integer) @number
(float) @number
(boolean) @boolean
(null) @constant.builtin
(host_body) @string.special

; -------------------------------------------------------------------- types
(type_identifier (identifier) @type)
(generic_type name: (identifier) @type)
(type_declaration name: (identifier) @type)
(variant_case name: (identifier) @constructor)
(type_parameters (identifier) @type.parameter)

; --------------------------------------------------------------- declarations
(service_declaration name: (identifier) @type)
(component_declaration name: (identifier) @type)
(function_declaration name: (identifier) @function)
(extern_declaration name: (identifier) @function)
(method_signature name: (identifier) @function.method)
(provide_method name: (identifier) @function.method)

(binding service: (identifier) @type)
(requires_clause (binding local: (identifier) @variable))
(provides_clause (binding local: (identifier) @variable))

; --------------------------------------------------------------- parameters
(typed_parameter name: (identifier) @variable.parameter)
(parameter name: (identifier) @variable.parameter)
(config_field name: (identifier) @property)
(record_type_field name: (identifier) @property)
(record_entry name: (identifier) @property)
(config_argument name: (identifier) @property)
(fault_config_field name: (identifier) @property)

; ------------------------------------------------------------------- calls
(call_expression
  function: (identifier) @function.call)
(call_expression
  function: (member_expression property: (identifier) @function.method.call))
(member_expression property: (identifier) @property)
(optional_member_expression property: (identifier) @property)

; effects / emissions / capabilities — revl's defining surface
(method_modifier) @keyword.effect
(capability_list (identifier) @label)

; ------------------------------------------------------------------- match
(match_pattern case: (identifier) @constructor)
(match_pattern binding: (identifier) @variable.parameter)

; ------------------------------------------------------------------- config
(config) @variable.builtin

; ------------------------------------------------------------------ operators
[
  "=" "+=" "-=" "*=" "/=" "%="
  "==" "===" "!=" "!==" "<" ">" "<=" ">="
  "+" "-" "*" "/" "%"
  "&&" "||" "??" "!"
  "^" "&" "~" "<<" ">>"
  "->" "=>" "?." "?" ":" "|"
] @operator

; --------------------------------------------------------------- punctuation
[ "(" ")" "{" "}" "[" "]" ] @punctuation.bracket
[ "," "." "@" ] @punctuation.delimiter

; ------------------------------------------------------------------ keywords
[
  "service" "component" "requires" "provides" "config"
  "provide" "fn" "return" "type" "use" "pub" "extern"
  "test" "as"
] @keyword

[ "let" "var" ] @keyword

[ "if" "else" "while" "for" "of" "match" ] @keyword.control

[
  "effect" "undo" "emit" "emission" "compensate"
  "isolate" "intercept" "realm" "in" "with" "spawn"
  "acquire" "pure" "await" "async" "fail" "verified" "commutative"
] @keyword.effect

"hole" @keyword.debug
"assert" @keyword.exception

; contextual statement keywords (lifecycle / fault tests)
[ "load" "unload" "call" ] @keyword
[ "fault" "lifecycle" ] @keyword
