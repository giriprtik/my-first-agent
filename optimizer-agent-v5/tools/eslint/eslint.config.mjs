// Config used by scanners.py for every project (the project's own ESLint config is not used, because its
// plugins are not installed in an uploaded .zip). Rules are chosen for bugs, security and accessibility,
// not formatting.
import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";
import security from "eslint-plugin-security";
import noUnsanitized from "eslint-plugin-no-unsanitized";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import jsxA11y from "eslint-plugin-jsx-a11y";

export default tseslint.config(
  { ignores: ["**/node_modules/**", "**/dist/**", "**/build/**", "**/*.min.js", "**/vendor/**", "**/coverage/**"] },
  {
    files: ["**/*.{js,mjs,cjs,jsx,ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    plugins: { security, "no-unsanitized": noUnsanitized, react, "react-hooks": reactHooks, "jsx-a11y": jsxA11y },
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: { ...globals.browser, ...globals.node, ...globals.jest, ...globals.mocha },
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    settings: { react: { version: "detect" } },
    linterOptions: { reportUnusedDisableDirectives: "off" },
    rules: {
      // security
      ...security.configs.recommended.rules,
      "security/detect-object-injection": "off",          // flags every obj[key]; almost all false positives
      "no-unsanitized/method": "error",
      "no-unsanitized/property": "error",
      "no-eval": "error",
      "no-implied-eval": "error",
      "no-new-func": "error",
      "react/no-danger": "warn",
      "react/jsx-no-target-blank": "error",
      "react/jsx-no-script-url": "error",
      // React correctness
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "react/jsx-key": "error",
      "react/no-direct-mutation-state": "error",
      "react/no-unknown-property": "error",
      // accessibility
      ...jsxA11y.flatConfigs.recommended.rules,
      // non-standard / risky JS & TS
      "eqeqeq": ["warn", "smart"],
      "no-var": "warn",
      "prefer-const": "warn",
      "no-throw-literal": "error",
      "@typescript-eslint/no-explicit-any": "warn",
      "@typescript-eslint/no-unused-vars": ["warn", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
      "@typescript-eslint/ban-ts-comment": "warn",
      "@typescript-eslint/no-require-imports": "off",
      "no-unused-vars": "off",
      "no-undef": "off",                                   // TypeScript and bundlers handle globals better
    },
  },
);
