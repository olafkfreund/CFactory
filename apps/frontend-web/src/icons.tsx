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
export const IconSettings = (p: IconProps) => (
  <Svg {...p}><path d="M4 7h9M19 7h1M4 17h1M11 17h9" /><circle cx="16" cy="7" r="2.4" /><circle cx="8" cy="17" r="2.4" /></Svg>
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
export const IconTokens = (p: IconProps) => (
  <Svg {...p}><ellipse cx="12" cy="6" rx="8" ry="3" /><path d="M4 6v6c0 1.66 3.58 3 8 3s8-1.34 8-3V6" /><path d="M4 12v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6" /></Svg>
);
export const IconEdit = (p: IconProps) => (
  <Svg {...p}><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" /></Svg>
);
export const IconRunning = (p: IconProps) => (
  <Svg {...p}><path d="M21 12a9 9 0 1 1-6.2-8.6" /><path d="M9 12l2 2 4.5-4.5" /></Svg>
);

// Factory glyphs for the Services view (mirror the pipeline stage icons).
export const IconControlTower = (p: IconProps) => (
  <Svg {...p}><path d="M12 8v12" /><path d="M8 20h8" /><circle cx="12" cy="5.5" r="2" /><path d="M7.5 3a6 6 0 0 0 0 5M16.5 3a6 6 0 0 1 0 5" /></Svg>
);
export const IconDocument = (p: IconProps) => (
  <Svg {...p}><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5" /><path d="M9 13h6M9 17h4" /></Svg>
);
export const IconRobot = (p: IconProps) => (
  <Svg {...p}><rect x="5" y="8" width="14" height="11" rx="3" /><path d="M12 4v4" /><circle cx="12" cy="3.4" r="1" /><circle cx="9.5" cy="13" r="1.2" /><circle cx="14.5" cy="13" r="1.2" /><path d="M10 16h4" /><path d="M3 12v3M21 12v3" /></Svg>
);
export const IconFlask = (p: IconProps) => (
  <Svg {...p}><path d="M9 3h6M10 3v6l-5.2 9.3A2 2 0 0 0 6.6 21h10.8a2 2 0 0 0 1.8-2.7L14 9V3" /><path d="M7 15h10" /></Svg>
);

export const IconClock = (p: IconProps) => (
  <Svg {...p} size={13}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></Svg>
);
export const IconExternal = (p: IconProps) => (
  <Svg {...p}><path d="M14 4h6v6" /><path d="M20 4l-9 9" /><path d="M19 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h5" /></Svg>
);
export const IconCheck = (p: IconProps) => (
  <Svg {...p} size={13}><path d="M20 6L9 17l-5-5" /></Svg>
);
