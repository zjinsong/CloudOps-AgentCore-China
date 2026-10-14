import ReactDOM from "react-dom/client";
import "@cloudscape-design/global-styles/index.css";
import App from "./App";

// 注意:不用 StrictMode —— 它会让 setState updater 在 dev 下被调用两次,
// 与我们的流式增量累加冲突(导致 delta 重复)。
ReactDOM.createRoot(document.getElementById("root")!).render(<App />);
