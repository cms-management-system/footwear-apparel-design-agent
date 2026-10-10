import tseslint from "typescript-eslint";
import hooks from "eslint-plugin-react-hooks";
import a11y from "eslint-plugin-jsx-a11y";
// All partner frontend application and tests are in this delivery boundary.
const files = ["components/**/*.{ts,tsx}", "lib/**/*.{ts,tsx}", "app/**/*.{ts,tsx}", "tests/**/*.{ts,tsx}"];
export default tseslint.config(
  ...tseslint.configs.recommended.map(config => ({ ...config, files })),
  { files, plugins: { "react-hooks": hooks, "jsx-a11y": a11y }, rules: { ...hooks.configs.recommended.rules, ...a11y.configs.recommended.rules } },
);
