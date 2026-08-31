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