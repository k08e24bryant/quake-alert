import { BMKG_URL, DISCLAIMER, SOURCE_NOTICE } from "@/lib/constants";

export function Footer() {
  return (
    <footer className="site-footer">
      <div className="container">
        <p className="disclaimer">{DISCLAIMER}</p>
        <p>
          <a href={BMKG_URL} target="_blank" rel="noopener noreferrer">
            {SOURCE_NOTICE}
          </a>
          <span aria-hidden="true"> · </span>
          <span>
            Peta:{" "}
            <a
              href="https://www.openstreetmap.org/copyright"
              target="_blank"
              rel="noopener noreferrer"
            >
              © kontributor OpenStreetMap
            </a>
          </span>
        </p>
      </div>
    </footer>
  );
}
