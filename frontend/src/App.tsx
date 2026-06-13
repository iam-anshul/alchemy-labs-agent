import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { SessionAuth } from "supertokens-auth-react/recipe/session";
import {
  canHandleRoute,
  getRoutingComponent,
} from "supertokens-auth-react/ui";

import { EmailPasswordPreBuiltUI } from "./auth";

const RunPage = lazy(() => import("./pages/RunPage"));
const WorkspacePage = lazy(() => import("./pages/WorkspacePage"));
const WorkspacesPage = lazy(() => import("./pages/WorkspacesPage"));

function ProductRoutes() {
  return (
    <SessionAuth>
      <Suspense fallback={<div className="route-loading">Loading Serca...</div>}>
        <Routes>
          <Route path="/workspaces" element={<WorkspacesPage />} />
          <Route
            path="/workspaces/:workspaceId"
            element={<WorkspacePage />}
          />
          <Route
            path="/workspaces/:workspaceId/runs/:runId"
            element={<RunPage />}
          />
          <Route path="*" element={<Navigate to="/workspaces" replace />} />
        </Routes>
      </Suspense>
    </SessionAuth>
  );
}

export default function App() {
  if (canHandleRoute([EmailPasswordPreBuiltUI])) {
    return getRoutingComponent([EmailPasswordPreBuiltUI]);
  }

  return <ProductRoutes />;
}
