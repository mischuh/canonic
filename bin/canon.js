#!/usr/bin/env node
// Placeholder stub for the @mischuh/canon npm package.
// Real npm distribution of the canonic CLI is not implemented yet — see
// SPEC-E1-foundation-config-distribution.md §5 (open question: npm scope
// and binary-distribution mechanism). Install the actual CLI via pip/uv
// until that lands.

console.error(
  "canon is not yet distributed via npm. Install it instead with:\n" +
    "  pip install canonic\n" +
    "or:\n" +
    "  uv tool install canonic\n" +
    "See SPEC-E1-foundation-config-distribution.md §5 for distribution status."
);
process.exit(1);
