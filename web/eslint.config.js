import js from '@eslint/js';
import prettier from 'eslint-config-prettier';
import { createTypeScriptImportResolver } from 'eslint-import-resolver-typescript';
import importX from 'eslint-plugin-import-x';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import unusedImports from 'eslint-plugin-unused-imports';
import { defineConfig, globalIgnores } from 'eslint/config';
import globals from 'globals';
import tseslint from 'typescript-eslint';

// Single source of truth for FE lint rules. Posture mirrored from
// factorbacktest/frontend-v2:
//
//   - We let the AI write the code, so the rules catch bug classes
//     (floating promises, misused promises, cyclic imports, dead
//     handlers) rather than style. Prettier owns style.
//   - `npm run lint` runs with `--max-warnings 0` so anything below
//     blocks. There's no "warn that nobody reads" tier.
//   - One structural rule: `max-lines: 700`. Combined with
//     `import-x/no-cycle` it physically prevents the god-component
//     pattern.
export default defineConfig([
  globalIgnores(['dist', 'node_modules']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.strict,
      tseslint.configs.stylistic,
      reactHooks.configs['recommended-latest'],
      reactRefresh.configs.vite,
      // Must be last: turns off any stylistic rules that would fight
      // Prettier. Don't add rules below that re-enable formatting.
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

      // Auto-strip dead imports on `--fix`.
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

      // Centralize HTTP at src/lib/api.ts. Every other call to global
      // `fetch` would silently bypass our error/JSON handling. The
      // override below re-allows fetch inside src/lib/api.ts.
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
