import js from '@eslint/js';

export default [
  { ignores: ['dist', 'node_modules'] },
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      globals: { ...globals.browser, console: 'readonly' },
      parser: await import('typescript-eslint').then((m) => m.ESLINT_TYPESCRIPT_PARSER),
    },
    plugins: {
      'react-hooks': await import('eslint-plugin-react-hooks'),
      'react-refresh': await import('eslint-plugin-react-refresh'),
    },
    rules: {
      'no-unused-vars': 'off',
      'no-console': 'off',
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      'react-refresh/only-export-components': 'warn',
    },
  },
];