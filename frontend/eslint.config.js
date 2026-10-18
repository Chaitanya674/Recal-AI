import js from '@eslint/js'
export default [
  js.configs.recommended,
  { files: ['src/**/*.jsx', 'src/**/*.js'],
    languageOptions: { ecmaVersion: 2022, sourceType: 'module',
      globals: { window: 'readonly', document: 'readonly', fetch: 'readonly',
                 TextDecoder: 'readonly', alert: 'readonly', setInterval: 'readonly',
                 clearInterval: 'readonly', setTimeout: 'readonly', clearTimeout: 'readonly',
                 Date: 'readonly', console: 'readonly' } },
    rules: { 'no-unused-vars': 'warn' } },
]
