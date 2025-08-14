# Noir Parser

A parser that processes Noir language files into logical code chunks for zero-knowledge circuit development. Noir is Aztec Network's domain-specific language for writing ZK circuits with Rust-like syntax.

## What We Capture

For each code chunk, we track:

- Type (function, struct, trait, impl, enum, mod, global)
- Code content with doc comments and attributes
- Noir-specific modifiers (unconstrained, pub visibility)
- Line numbers for source mapping
- Context preservation for ZK-specific constructs

## Data Structure

Each chunk is represented as:

```ts
interface Data {
  title: string; // Example: "unconstrained fn: verify (4-12)"
  content: string; // The actual code with imports and comments
  start: number; // First line number
  end: number; // Last line number
}
```

## How It Works

1. **Main Parser (`parseNoir`)**
   - Formats code using rustfmt (Noir syntax is Rust-like)
   - Scans for:
     - Functions (regular and unconstrained)
     - Structs with Field types
     - Traits and implementations
     - Enums and modules
     - Global constants

2. **Noir-Specific Features**
   - **Unconstrained Functions**: Detected and labeled separately
   - **Field Types**: ZK-specific numeric type handling
   - **Public/Private Visibility**: Parameter and return type modifiers
   - **Global Constants**: `global` keyword instead of `const`
   - **Assert Statements**: ZK constraint detection

3. **Functions (`parseNoirFunction`)**
   - Detects regular and unconstrained functions
   - Handles pub parameters: `fn main(x: Field, y: pub Field)`
   - Includes doc comments and attributes
   - Tracks ZK-specific patterns

4. **Structs (`parseNoirStruct`)**
   - Captures Field types and visibility modifiers
   - Preserves ZK-optimized data structures
   - Handles public/private field declarations

5. **Global Constants (`parseNoirGlobal`)**
   - Parses `global` declarations instead of `const`
   - Handles compile-time constants for circuits

## Key Differences from Rust

- **Unconstrained Functions**: `unconstrained fn` for expensive computations
- **Field Type**: Primary numeric type for ZK proofs
- **Public Parameters**: `pub` modifier on function parameters
- **Assert Statements**: `assert()` for ZK constraints instead of panic
- **No Early Returns**: Circuit-compatible control flow
- **Global Constants**: `global` keyword instead of `const`

## Example

```noir
// Global constant
global MAX_USERS: Field = 1000;

// Regular constrained function
fn verify_age(age: u8, min_age: pub u8) -> pub bool {
    assert(age >= min_age);
    true
}

// Unconstrained function for heavy computation
unconstrained fn hash_computation(data: [u8; 32]) -> Field {
    // Complex operations outside circuit
    pedersen_hash(data)
}

struct User {
    id: Field,
    pub age: u8,        // Public field
    balance: Field,     // Private field
}

impl User {
    fn new(id: Field, age: u8) -> Self {
        User { id, age, balance: 0 }
    }
    
    unconstrained fn get_balance(self) -> Field {
        self.balance
    }
}
```

## Parsing Examples

### Unconstrained Functions

Input:
```noir
/// Heavy computation outside circuit
unconstrained fn expensive_calc(data: [Field; 100]) -> Field {
    // Complex operations
    data[0] + data[99]
}
```

Produces:
```ts
{
  title: "unconstrained fn: expensive_calc (1-4)",
  content: "/// Heavy computation outside circuit\nunconstrained fn expensive_calc(data: [Field; 100]) -> Field {\n    data[0] + data[99]\n}",
  start: 1,
  end: 4
}
```

### Public/Private Parameters

Input:
```noir
fn main(private_input: Field, public_input: pub Field) -> pub Field {
    assert(private_input > 0);
    private_input + public_input
}
```

Produces:
```ts
{
  title: "fn: main (1-4)",
  content: "fn main(private_input: Field, public_input: pub Field) -> pub Field {\n    assert(private_input > 0);\n    private_input + public_input\n}",
  start: 1,
  end: 4
}
```

### Global Constants

Input:
```noir
/// Maximum number of transactions per block
global MAX_TXS: Field = 1024;
```

Produces:
```ts
{
  title: "global: MAX_TXS (1-2)",
  content: "/// Maximum number of transactions per block\nglobal MAX_TXS: Field = 1024;",
  start: 1,
  end: 2
}
```

## Use Cases

The parser helps with:

- ZK circuit analysis and optimization
- Noir code documentation generation
- Constraint system understanding
- Circuit compilation tooling
- Educational ZK proof materials

## ZK-Specific Optimizations

- Recognizes Field types for arithmetic circuits
- Distinguishes constrained vs unconstrained code
- Preserves public/private data distinctions
- Handles assert statements as ZK constraints
- Maintains circuit-compatible patterns