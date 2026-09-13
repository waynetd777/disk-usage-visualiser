export interface Volume {
  name: string;
  total: number;
  free: number;
  used: number;
}

export interface ScanSummary {
  finished: string;
  duration_ms: number;
  items: number;
  size: number;
  apparent: number;
  denied: number;
  cache_bytes: number;
}

export interface Recent {
  path: string;
  name: string;
  size: number;
  items: number;
  when: string;
}

export interface AppInfo {
  root: string;
  root_name: string;
  scanning: boolean;
  loading: boolean;
  scan: ScanSummary | null;
  volume: Volume;
  recent: Recent[];
  full_disk_access: boolean;
  home: string;
}

export interface Progress {
  scanning: boolean;
  items: number;
  bytes: number;
  dirs: number;
  denied: number;
  current: string;
  elapsed_ms: number;
  expected_items: number | null;
}

/// One folder as the treemap draws it. `depth` is from the scan root (root = 0).
export interface View {
  name: string;
  path: string;
  size: number;
  apparent: number;
  files: number;
  loose_size: number;
  loose_apparent: number;
  items: number;
  cloud: boolean;
  denied: number;
  depth: number;
  kids: View[];
  more: number;
  more_size: number;
  more_apparent: number;
}
