import js from '@eslint/js';
import globals from 'globals';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import importX from 'eslint-plugin-import-x';
import { createTypeScriptImportResolver } from 'eslint-import-resolver-typescript';
import unusedImports from 'eslint-plugin-unused-imports';
import prettier from 'eslint-config-prettier';
import tseslint from 'typescript-eslint';
import { defineConfig, globalIgnores } from 'eslint/config';

// Single source of truth for FE lint rules. The high-level posture:
//
//   - We let the AI write the code, so the rules are tuned to catch
//     classes of mistakes that get past humans (floating promises,
//     misused promises, cyclic imports, dead handlers) rather than
//     style. Style is owned by Prettier.
//   - `npm run lint` runs with `--max-warnings 0`, so anything below
//     blocks CI. There is no "warn that nobody reads" tier — if a rule
//     isn't worth blocking on, it isn't on.
//   - One structural rule: `max-lines: 700`. Combined with
//     `import-x/no-cycle` it physically prevents the "1000-line god
//     component" pattern.
export default defineConfig([
  globalIgnores(['dist', 'node_modules']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.strict,
      tseslint.configs.stylistic,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
      // Must be last: turns off any stylistic rules that would fight
      // Prettier. Don't add rules below that re-enable formatting
      // concerns — keep formatting in Prettier.
      prettier,
    ],
    plugins: {
      'import-x': importX,
      'unused-imports': unusedImports,
    },
    languageOptions: {
      globals: globals.browser,
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    settings: {
      'import-x/resolver-next': [
        createTypeScriptImportResolver({
          project: ['./tsconfig.app.json', './tsconfig.node.json'],
          noWarnOnMultipleProjects: true,
        }),
      ],
    },
    rules: {
      // Hard structural cap — keeps files focused and reviewable.
      'max-lines': ['error', { max: 700, skipBlankLines: true, skipComments: true }],

      // Bug-class rules.
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/no-floating-promises': 'error',
      '@typescript-eslint/no-misused-promises': 'error',
      '@typescript-eslint/consistent-type-imports': 'error',
      '@typescript-eslint/switch-exhaustiveness-check': 'error',
      'react-hooks/exhaustive-deps': 'error',
      'import-x/no-cycle': ['error', { maxDepth: 10 }],
      'import-x/no-duplicates': 'error',
      'import-x/order': [
        'error',
        {
          groups: [
            ['builtin', 'external'],
            ['internal', 'parent', 'sibling', 'index'],
          ],
          'newlines-between': 'always',
          alphabetize: { order: 'asc', caseInsensitive: true },
        },
      ],

      // Replaces `@typescript-eslint/no-unused-vars` with the
      // unused-imports plugin so `--fix` can auto-strip dead imports.
      '@typescript-eslint/no-unused-vars': 'off',
      'unused-imports/no-unused-imports': 'error',
      'unused-imports/no-unused-vars': [
        'error',
        { vars: 'all', varsIgnorePattern: '^_', args: 'after-used', argsIgnorePattern: '^_' },
      ],

      'no-alert': 'error',
      'no-console': ['warn', { allow: ['warn', 'error'] }],
      eqeqeq: ['error', 'always'],
      'prefer-const': 'error',

      // Centralize HTTP at src/lib/api.ts (apiClient). Every call to
      // global `fetch` outside that file would silently bypass our
      // credentials/JSON/error-shape handling.
      'no-restricted-syntax': [
        'error',
        {
          selector: "CallExpression[callee.name='fetch']",
          message: 'Use the apiClient from @/lib/api instead of calling fetch directly.',
        },
      ],
    },
  },
  {
    // The lone exception: src/lib/api.ts *is* the wrapper around fetch.
    files: ['src/lib/api.ts'],
    rules: {
      'no-restricted-syntax': 'off',
    },
  },
]);
