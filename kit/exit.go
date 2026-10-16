package kit

import (
	"errors"
	"fmt"
)

// Contract names the version of the grammar every app speaks.
const Contract = "zeo-cli/v1"

// ExitCode is a process exit code from the one table every app shares.
type ExitCode int

// The table of rev d section 4. An app that needs another meaning reports it
// in its own documents' fields, never in the exit code.
const (
	ExitOK          ExitCode = 0 // did what was asked
	ExitFailed      ExitCode = 1 // something failed; stderr names it
	ExitUsage       ExitCode = 2 // the command line is wrong
	ExitRefused     ExitCode = 3 // refused before acting; nothing changed
	ExitNotFound    ExitCode = 4 // the thing asked about does not exist
	ExitTimeout     ExitCode = 5 // a deadline passed
	ExitUnavailable ExitCode = 6 // a service it needs did not answer
	ExitNotReady    ExitCode = 7 // doctor found a failing prerequisite
)

// inTable reports whether the code is a row of the table.
func (c ExitCode) inTable() bool { return c >= ExitOK && c <= ExitNotReady }

// ExitInfo is one row of the exit table, as help --json prints it.
type ExitInfo struct {
	Code    ExitCode `json:"code"`
	Name    string   `json:"name"`
	Meaning string   `json:"meaning"`
}

// ExitTable is the whole table, in code order.
func ExitTable() []ExitInfo {
	return []ExitInfo{
		{ExitOK, "ok", "Did what was asked."},
		{ExitFailed, "failed", "Something failed. stderr names it."},
		{ExitUsage, "usage", "The command line is wrong."},
		{ExitRefused, "refused", "Refused before acting, on a precondition. Nothing changed."},
		{ExitNotFound, "not_found", "The thing asked about does not exist."},
		{ExitTimeout, "timeout", "A deadline passed."},
		{ExitUnavailable, "unavailable", "A service it needs did not answer."},
		{ExitNotReady, "not_ready", "doctor found a failing prerequisite."},
	}
}

// Error is a failure that carries its exit code. Message is for stderr and
// must never hold a secret or an echoed input value.
type Error struct {
	Code    ExitCode
	Message string
	// Reason is the one lowercase token of a refusal ("busy", "signature").
	Reason string
}

func (e *Error) Error() string { return e.Message }

func newError(code ExitCode, format string, args ...any) *Error {
	return &Error{Code: code, Message: fmt.Sprintf(format, args...)}
}

// Failed is exit 1.
func Failed(format string, args ...any) error { return newError(ExitFailed, format, args...) }

// Usagef is exit 2.
func Usagef(format string, args ...any) error { return newError(ExitUsage, format, args...) }

// Refused is exit 3, with the reason token a RESULT line carries.
func Refused(reason, format string, args ...any) error {
	e := newError(ExitRefused, format, args...)
	e.Reason = reason
	return e
}

// NotFound is exit 4.
func NotFound(format string, args ...any) error { return newError(ExitNotFound, format, args...) }

// Timeout is exit 5.
func Timeout(format string, args ...any) error { return newError(ExitTimeout, format, args...) }

// Unavailable is exit 6.
func Unavailable(format string, args ...any) error { return newError(ExitUnavailable, format, args...) }

// ExitOnly ends a verb with a code and no message, for a verb that has
// already printed its own document.
func ExitOnly(code ExitCode) error {
	if code == ExitOK {
		return nil
	}
	return &Error{Code: code}
}

// codeOf maps any error to its exit code: an *Error keeps its own, anything
// else is a failure (exit 1).
func codeOf(err error) ExitCode {
	if err == nil {
		return ExitOK
	}
	var e *Error
	if errors.As(err, &e) {
		return e.Code
	}
	return ExitFailed
}
