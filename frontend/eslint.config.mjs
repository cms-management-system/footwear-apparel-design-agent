import tseslint from "typescript-eslint";
import hooks from "eslint-plugin-react-hooks";
import a11y from "eslint-plugin-jsx-a11y";
// Incremental lint boundary: the existing workbench predates ESLint. All newly added Agent UI is checked.
const files = ["components/design/**/*.{ts,tsx}", "lib/agent-api.ts", "lib/cms.ts", "app/layout.tsx", "app/design/**/*.tsx", "app/designs/**/*.tsx", "app/page.tsx", "app/requirements/**/*.tsx", "tests/design-*.test.tsx", "tests/model-3d.test.tsx"];
export default tseslint.config(
  ...tseslint.configs.recommended.map(config => ({ ...config, files })),
  { files, plugins: { "react-hooks": hooks, "jsx-a11y": a11y }, rules: { ...hooks.configs.recommended.rules, ...a11y.configs.recommended.rules } },
);
