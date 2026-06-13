import { LogOut } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import Session from "supertokens-auth-react/recipe/session";

import "./AppShell.css";

interface Crumb {
  label: string;
  to?: string;
}

interface AppShellProps {
  crumbs: Crumb[];
  actions?: ReactNode;
  children: ReactNode;
  contentClassName?: string;
}

export default function AppShell({
  crumbs,
  actions,
  children,
  contentClassName = "",
}: AppShellProps) {
  async function handleSignOut() {
    await Session.signOut();
    window.location.assign("/auth");
  }

  return (
    <div className="app-shell">
      <header className="top-bar">
        <div className="top-bar__identity">
          <Link className="brand" to="/workspaces" aria-label="Alchemy Labs workspaces">
            <span className="brand__mark" aria-hidden="true" />
            <span>alchemy labs</span>
          </Link>
          <nav className="breadcrumbs" aria-label="Breadcrumb">
            {crumbs.map((crumb, index) => (
              <span className="breadcrumbs__item" key={`${crumb.label}-${index}`}>
                <span className="breadcrumbs__separator" aria-hidden="true">/</span>
                {crumb.to ? (
                  <Link to={crumb.to}>{crumb.label}</Link>
                ) : (
                  <span aria-current="page">{crumb.label}</span>
                )}
              </span>
            ))}
          </nav>
        </div>
        <div className="top-bar__actions">
          {actions}
          <button
            className="icon-button"
            type="button"
            onClick={handleSignOut}
            aria-label="Sign out"
            title="Sign out"
          >
            <LogOut size={15} />
          </button>
        </div>
      </header>
      <main className={`app-shell__content ${contentClassName}`.trim()}>
        {children}
      </main>
    </div>
  );
}
