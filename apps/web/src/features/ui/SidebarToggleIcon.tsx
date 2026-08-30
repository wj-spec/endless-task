type SidebarToggleIconProps = {
  expanded: boolean;
};

export function SidebarToggleIcon({ expanded }: SidebarToggleIconProps) {
  return (
    <svg
      aria-hidden="true"
      className="sidebar-toggle-icon"
      fill="none"
      viewBox="0 0 20 20"
    >
      <rect height="14" rx="2.5" stroke="currentColor" width="15" x="2.5" y="3" />
      <path d="M7.5 3.5v13" stroke="currentColor" />
      <path
        d={expanded ? "m12.75 7-2.5 3 2.5 3" : "m10.75 7 2.5 3-2.5 3"}
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}