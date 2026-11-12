import { createRoot } from "react-dom/client";
import { App } from "./App";
import { createRuntime } from "./runtime";

const root = document.getElementById("root");
if (!root) throw new Error("#root missing");
createRoot(root).render(<App runtime={createRuntime()} />);
