import { Link } from "react-router";

import { PageTitle } from "../components/common";

export function NotFoundPage() {
  return (
    <>
      <PageTitle>Page not found</PageTitle>
      <p>
        <Link to="/">Back to the runs</Link>
      </p>
    </>
  );
}
