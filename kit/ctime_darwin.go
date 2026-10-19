//go:build darwin

package kit

import "syscall"

// ctimeOf is the inode change time: it moves on every write, chmod and rename,
// and a caller cannot set it back (unlike the mtime, which os.Chtimes restores).
func ctimeOf(st *syscall.Stat_t) int64 { return int64(st.Ctimespec.Sec)*1e9 + int64(st.Ctimespec.Nsec) }
