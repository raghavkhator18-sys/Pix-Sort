type IconProps = { size?: number; strokeWidth?: number; color?: string; label?: string };

function Icon({ children, size = 20, strokeWidth = 2, color = "currentColor", label }: IconProps & { children: React.ReactNode }) {
  return <svg aria-hidden={label ? undefined : true} aria-label={label} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round">{children}</svg>;
}

export function Info(props: IconProps) {
  return <Icon {...props}><circle cx="12" cy="12" r="9" /><path d="M12 11v5M12 8h.01" /></Icon>;
}

export function FolderOpen(props: IconProps) {
  return <Icon {...props}><path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H10l2 2h6.5A2.5 2.5 0 0 1 21 9.5v7A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5z" /><path d="M3.5 9h17" /></Icon>;
}

export function Images(props: IconProps) {
  return <Icon {...props}><rect x="3" y="5" width="15" height="15" rx="2" /><path d="m3 16 4-4 3 3 2-2 6 6M7 9h.01" /><path d="M8 3h11a2 2 0 0 1 2 2v11" /></Icon>;
}

export function Copy(props: IconProps) {
  return <Icon {...props}><rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" /></Icon>;
}

export function ArrowRight(props: IconProps) {
  return <Icon {...props}><path d="M5 12h14M13 6l6 6-6 6" /></Icon>;
}

export function ArrowLeft(props: IconProps) {
  return <Icon {...props}><path d="M19 12H5M11 18l-6-6 6-6" /></Icon>;
}

export function Check(props: IconProps) {
  return <Icon {...props}><path d="m5 12 4 4L19 6" /></Icon>;
}

export function Star(props: IconProps) {
  return <Icon {...props}><path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-2.9-5.6 2.9 1.1-6.2L3 9.6l6.2-.9z" /></Icon>;
}

export function Trash2(props: IconProps) {
  return <Icon {...props}><path d="M4 7h16M10 11v6M14 11v6M6 7l1 14h10l1-14M9 7V4h6v3" /></Icon>;
}

export function AlertTriangle(props: IconProps) {
  return <Icon {...props}><path d="m12 3 9 17H3z" /><path d="M12 9v4M12 17h.01" /></Icon>;
}

export function ChevronUp(props: IconProps) {
  return <Icon {...props}><path d="m6 15 6-6 6 6" /></Icon>;
}

export function GoogleDrive(props: IconProps) {
  return (
    <Icon strokeWidth={props.strokeWidth ?? 1.8} {...props}>
      <path d="M8.5 3.5h6l7 12.1h-6z" />
      <path d="M8.5 3.5 2 15.6l3 5.1 6.5-12.1z" />
      <path d="M5 20.7h13l3.5-5.1H8z" />
    </Icon>
  );
}

export function Cloud(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z" />
    </Icon>
  );
}

export function X(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M18 6 6 18M6 6l12 12" />
    </Icon>
  );
}
