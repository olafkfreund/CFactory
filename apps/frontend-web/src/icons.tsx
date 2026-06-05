// Minimal inline SVG icon set (lucide-style), no dependency.
import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function Svg({ children, size = 18, ...rest }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      {...rest}
    >
      {children}
    </svg>
  );
}

export const IconBrand = (p: IconProps) => (
  <Svg {...p}><circle cx="12" cy="12" r="9" /><circle cx="12" cy="12" r="3.5" /><path d="M12 1.5v3M12 19.5v3M1.5 12h3M19.5 12h3" /></Svg>
);
export const IconPipeline = (p: IconProps) => (
  <Svg {...p}><rect x="3" y="4" width="5" height="16" rx="1" /><rect x="9.5" y="4" width="5" height="10" rx="1" /><rect x="16" y="4" width="5" height="13" rx="1" /></Svg>
);
export const IconCopilot = (p: IconProps) => (
  <Svg {...p}><path d="M12 3l1.8 4.7L18.5 9.5 13.8 11.3 12 16l-1.8-4.7L5.5 9.5l4.7-1.8z" /><path d="M19 15l.7 1.8L21.5 17.5l-1.8.7L19 20l-.7-1.8L16.5 17.5l1.8-.7z" /></Svg>
);
export const IconInsights = (p: IconProps) => (
  <Svg {...p}><path d="M3 12h3l2.5 6 5-13 2.5 7H21" /></Svg>
);
export const IconAudit = (p: IconProps) => (
  <Svg {...p}><path d="M12 2l8 3v6c0 5-3.5 8.5-8 11-4.5-2.5-8-6-8-11V5z" /><path d="M9 12l2 2 4-4" /></Svg>
);
export const IconServices = (p: IconProps) => (
  <Svg {...p}><rect x="3" y="4" width="18" height="6" rx="1.5" /><rect x="3" y="14" width="18" height="6" rx="1.5" /><path d="M7 7h.01M7 17h.01" /></Svg>
);
export const IconRefresh = (p: IconProps) => (
  <Svg {...p}><path d="M21 12a9 9 0 1 1-2.6-6.4" /><path d="M21 4v5h-5" /></Svg>
);
export const IconLogout = (p: IconProps) => (
  <Svg {...p}><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><path d="M16 17l5-5-5-5M21 12H9" /></Svg>
);
export const IconClock = (p: IconProps) => (
  <Svg {...p} size={13}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></Svg>
);
export const IconCheck = (p: IconProps) => (
  <Svg {...p} size={13}><path d="M20 6L9 17l-5-5" /></Svg>
);
