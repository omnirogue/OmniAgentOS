# Sequence Expression Calculator Examples

Here are several expressions and their expected float results, as evaluated by the sequence expression calculator:

| Expression | Expected Result | Description |
| :--- | :--- | :--- |
| `2 + 3 * 4` | `14.0` | Operator precedence: `*` before `+` |
| `(2 + 3) * 4` | `20.0` | Parentheses override precedence |
| `2 ^ 3 ^ 2` | `512.0` | Right-associativity of `^` (`2 ^ (3 ^ 2)`) |
| `10 % 3` | `1.0` | Standard modulo (`math.fmod`) |
| `-10 % 3` | `-1.0` | Sign of modulo result follows left operand |
| `10 % -3` | `1.0` | Sign of modulo result follows left operand |
| `2.5 * 4 + 1.5` | `11.5` | Floating point literals and arithmetic |
| `(5 + 2) ^ 2 * 3` | `147.0` | Power and parentheses |

Errors:
- Unary minus like `-3` or `+5` are invalid and should raise `ParseError`. Use `0 - 3` instead.
- `1.` and `.5` are invalid number formats and should raise `LexError`.
