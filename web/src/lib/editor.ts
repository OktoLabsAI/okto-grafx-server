import { autocompletion, type CompletionContext, type CompletionResult } from "@codemirror/autocomplete";
import { EditorView } from "@codemirror/view";
import { Prec } from "@codemirror/state";
import { keymap } from "@codemirror/view";
import { oneDark } from "@codemirror/theme-one-dark";

/** The editor is dark like the console; the theme extension is applied here, once. */
export const oktoTheme = oneDark;

/**
 * Completion is fed from the store's own schema, so the console suggests the tables and columns that
 * exist rather than a generic SQL vocabulary — and refuses nothing: it is a hint list, not a
 * validator, because the engine is the authority on what parses.
 */
export function editorExtensions(words: string[]) {
  const source = (context: CompletionContext): CompletionResult | null => {
    const word = context.matchBefore(/[\w`:]*/);
    if (!word || (word.from === word.to && !context.explicit)) return null;
    return {
      from: word.from,
      options: [...new Set(words)]
        .filter(Boolean)
        .slice(0, 400)
        .map((label) => ({ label, type: "text" })),
      validFor: /^[\w`:]*$/,
    };
  };
  return [
    Prec.highest(
      keymap.of([
        {
          key: "Mod-Enter",
          run: () => {
            // The page-level handler owns the run; this only stops CodeMirror inserting a newline.
            return false;
          },
          preventDefault: false,
        },
      ]),
    ),
    autocompletion({ override: [source] }),
    EditorView.theme({
      "&": { backgroundColor: "transparent" },
      ".cm-scroller": { fontFamily: "var(--font-mono)" },
      ".cm-gutters": { backgroundColor: "#090b11", border: "none" },
    }),
  ];
}

