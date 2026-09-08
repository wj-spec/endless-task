import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & {
  size?: number;
};

const iconProps = ({ size = 18, ...props }: IconProps) => ({
  "aria-hidden": true as const,
  fill: "none",
  focusable: false as const,
  height: size,
  stroke: "currentColor",
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  strokeWidth: 1.8,
  viewBox: "0 0 24 24",
  width: size,
  ...props,
});

export function AddIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)}>
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}

export function BranchIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)}>
      <circle cx="7" cy="5" r="2" />
      <circle cx="17" cy="7" r="2" />
      <circle cx="7" cy="19" r="2" />
      <path d="M7 7v10M9 11h3a5 5 0 0 0 5-2" />
    </svg>
  );
}

export function ChevronIcon({
  direction = "right",
  ...props
}: IconProps & { direction?: "down" | "left" | "right" | "up" }) {
  const paths = {
    down: "m7 9 5 5 5-5",
    left: "m15 7-5 5 5 5",
    right: "m9 7 5 5-5 5",
    up: "m7 15 5-5 5 5",
  };
  return (
    <svg {...iconProps(props)}>
      <path d={paths[direction]} />
    </svg>
  );
}

export function CloseIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)}>
      <path d="M6 6l12 12M18 6 6 18" />
    </svg>
  );
}

export function FileIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)}>
      <path d="M7 3h7l4 4v14H7z" />
      <path d="M14 3v5h5" />
    </svg>
  );
}

export function FolderIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)}>
      <path d="M3 6h7l2 2h9v11H3z" />
    </svg>
  );
}

export function MoreIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)}>
      <circle cx="5" cy="12" r="1" fill="currentColor" stroke="none" />
      <circle cx="12" cy="12" r="1" fill="currentColor" stroke="none" />
      <circle cx="19" cy="12" r="1" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function RefreshIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)}>
      <path d="M20 11a8 8 0 1 0-2.3 5.7" />
      <path d="M20 5v6h-6" />
    </svg>
  );
}

export function SearchIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)}>
      <circle cx="11" cy="11" r="6.5" />
      <path d="m16 16 4 4" />
    </svg>
  );
}

export function SendIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)}>
      <path d="M12 19V5M6.5 10.5 12 5l5.5 5.5" />
    </svg>
  );
}

export function TerminalIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)}>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="m7 9 3 3-3 3M13 15h4" />
    </svg>
  );
}

export function GlobeIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)}>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" />
    </svg>
  );
}

export function WrenchIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)}>
      <path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L4 17l3 3 5.3-5.3a4 4 0 0 0 5.4-5.4l-2.7 2.7-2.6-2.6z" />
    </svg>
  );
}

export function BookIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)}>
      <path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z" />
      <path d="M4 19a2 2 0 0 1 2-2h13" />
    </svg>
  );
}

export function SparkleIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)}>
      <path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z" />
    </svg>
  );
}