import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { Shell } from "@/components/Shell";
import { Login } from "@/routes/Login";
import { Databases } from "@/routes/Databases";
import { DatabaseDetail } from "@/routes/DatabaseDetail";
import { Search } from "@/routes/Search";
import { Keys } from "@/routes/Keys";
import { Audit } from "@/routes/Audit";
import { Jobs } from "@/routes/Jobs";
import { Settings } from "@/routes/Settings";

// Sigma is the heaviest thing the console ships, and only one route needs WebGL: load it there.
const GraphView = lazy(() => import("@/routes/GraphView").then((module) => ({ default: module.GraphView })));
// CodeMirror is the other heavy one, and the console is the only place that edits a statement.
const QueryConsole = lazy(() => import("@/routes/QueryConsole").then((module) => ({ default: module.QueryConsole })));

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        element={
          <Shell>
            <Routes>
              <Route path="/" element={<Navigate replace to="/databases" />} />
              <Route path="/databases" element={<Databases />} />
              <Route path="/databases/:slug" element={<DatabaseDetail />} />
              <Route
                element={
                  <Suspense fallback={<p className="p-6 text-[12.5px] text-muted">loading the editor…</p>}>
                    <QueryConsole />
                  </Suspense>
                }
                path="/databases/:slug/query"
              />
              <Route path="/databases/:slug/search" element={<Search />} />
              <Route
                element={
                  <Suspense fallback={<p className="p-6 text-[12.5px] text-muted">loading the WebGL view…</p>}>
                    <GraphView />
                  </Suspense>
                }
                path="/databases/:slug/graph"
              />
              <Route path="/keys" element={<Keys />} />
              <Route path="/audit" element={<Audit />} />
              <Route path="/jobs" element={<Jobs />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="*" element={<Navigate replace to="/databases" />} />
            </Routes>
          </Shell>
        }
        path="/*"
      />
    </Routes>
  );
}
