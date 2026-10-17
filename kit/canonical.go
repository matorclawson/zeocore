package kit

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"sort"
	"strconv"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
)

// Canonical returns the RFC 8785 (JCS) form of one JSON document: keys sorted
// by UTF-16 code units, no insignificant whitespace, numbers in the ECMAScript
// form, and strings with the minimal escapes. A duplicate key, trailing data
// or a number that is not finite is an error.
func Canonical(doc []byte) ([]byte, error) {
	if !utf8.Valid(doc) {
		return nil, errors.New("canonical: the document is not valid UTF-8")
	}
	if loneSurrogate(doc) {
		return nil, errors.New("canonical: a string holds an unpaired surrogate escape")
	}
	dec := json.NewDecoder(bytes.NewReader(doc))
	dec.UseNumber()
	var out bytes.Buffer
	if err := canonValue(dec, &out); err != nil {
		return nil, err
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return nil, errors.New("canonical: trailing data after the document")
	}
	return out.Bytes(), nil
}

// MaxDigestBytes is the largest document Digest takes, the same bound as
// `zeocore digest` (1 MiB).
const MaxDigestBytes = 1 << 20

// loneSurrogate reports a \uD800 to \uDFFF escape that is not half of a
// valid pair. encoding/json turns such an escape into U+FFFD silently, where
// the Python side (rfc8785) refuses it.
func loneSurrogate(doc []byte) bool {
	hex4 := func(i int) (rune, bool) {
		if i+6 > len(doc) || doc[i] != '\\' || doc[i+1] != 'u' {
			return 0, false
		}
		var r rune
		for _, c := range doc[i+2 : i+6] {
			switch {
			case c >= '0' && c <= '9':
				r = r<<4 | rune(c-'0')
			case c >= 'a' && c <= 'f':
				r = r<<4 | rune(c-'a'+10)
			case c >= 'A' && c <= 'F':
				r = r<<4 | rune(c-'A'+10)
			default:
				return 0, false
			}
		}
		return r, true
	}
	for i := 0; i < len(doc); i++ {
		if doc[i] != '\\' {
			continue
		}
		if r, ok := hex4(i); ok {
			switch {
			case r >= 0xD800 && r < 0xDC00:
				if low, ok := hex4(i + 6); !ok || low < 0xDC00 || low > 0xDFFF {
					return true
				}
				i += 11
				continue
			case r >= 0xDC00 && r <= 0xDFFF:
				return true
			}
			i += 5
			continue
		}
		i++ // skip the escaped character
	}
	return false
}

// Digest is the sha256, in lowercase hex, of the canonical form of doc.
func Digest(doc []byte) (string, error) {
	if len(doc) > MaxDigestBytes {
		return "", errors.New("canonical: the document is larger than 1 MiB")
	}
	c, err := Canonical(doc)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(c)
	return hex.EncodeToString(sum[:]), nil
}

func canonValue(dec *json.Decoder, out *bytes.Buffer) error {
	tok, err := dec.Token()
	if err != nil {
		return fmt.Errorf("canonical: %w", err)
	}
	switch v := tok.(type) {
	case nil:
		out.WriteString("null")
	case bool:
		out.WriteString(strconv.FormatBool(v))
	case string:
		canonString(out, v)
	case json.Number:
		s, err := canonNumber(v)
		if err != nil {
			return err
		}
		out.WriteString(s)
	case json.Delim:
		if v == '[' {
			out.WriteByte('[')
			first := true
			for dec.More() {
				if !first {
					out.WriteByte(',')
				}
				first = false
				if err := canonValue(dec, out); err != nil {
					return err
				}
			}
			if _, err := dec.Token(); err != nil {
				return fmt.Errorf("canonical: %w", err)
			}
			out.WriteByte(']')
			return nil
		}
		type member struct {
			key   string
			value []byte
		}
		var members []member
		seen := map[string]bool{}
		for dec.More() {
			kt, err := dec.Token()
			if err != nil {
				return fmt.Errorf("canonical: %w", err)
			}
			key := kt.(string)
			if seen[key] {
				return fmt.Errorf("canonical: duplicate key %q", key)
			}
			seen[key] = true
			var val bytes.Buffer
			if err := canonValue(dec, &val); err != nil {
				return err
			}
			members = append(members, member{key, val.Bytes()})
		}
		if _, err := dec.Token(); err != nil {
			return fmt.Errorf("canonical: %w", err)
		}
		sort.Slice(members, func(i, j int) bool {
			return slicesCompareUTF16(members[i].key, members[j].key) < 0
		})
		out.WriteByte('{')
		for i, m := range members {
			if i > 0 {
				out.WriteByte(',')
			}
			canonString(out, m.key)
			out.WriteByte(':')
			out.Write(m.value)
		}
		out.WriteByte('}')
	}
	return nil
}

func slicesCompareUTF16(a, b string) int {
	ua, ub := utf16.Encode([]rune(a)), utf16.Encode([]rune(b))
	for i := 0; i < len(ua) && i < len(ub); i++ {
		if ua[i] != ub[i] {
			if ua[i] < ub[i] {
				return -1
			}
			return 1
		}
	}
	return len(ua) - len(ub)
}

func canonString(out *bytes.Buffer, s string) {
	out.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			out.WriteString(`\"`)
		case '\\':
			out.WriteString(`\\`)
		case '\b':
			out.WriteString(`\b`)
		case '\f':
			out.WriteString(`\f`)
		case '\n':
			out.WriteString(`\n`)
		case '\r':
			out.WriteString(`\r`)
		case '\t':
			out.WriteString(`\t`)
		default:
			if r < 0x20 {
				fmt.Fprintf(out, `\u%04x`, r)
			} else {
				out.WriteRune(r)
			}
		}
	}
	out.WriteByte('"')
}

// canonNumber formats a JSON number the way ECMAScript's Number.prototype.toString does.
func canonNumber(n json.Number) (string, error) {
	if !strings.ContainsAny(string(n), ".eE") {
		// zeocore's digest refuses an integer literal outside the safe range, so the
		// same input cannot give two different answers in the two languages.
		i, err := strconv.ParseInt(string(n), 10, 64)
		if err != nil || i > 1<<53-1 || i < -(1<<53-1) {
			return "", fmt.Errorf("canonical: integer %q is outside +/-(2^53-1)", string(n))
		}
	}
	f, err := strconv.ParseFloat(string(n), 64)
	if err != nil || math.IsInf(f, 0) || math.IsNaN(f) {
		return "", fmt.Errorf("canonical: number %q is not a finite float64", string(n))
	}
	if f == 0 {
		return "0", nil
	}
	neg := f < 0
	if neg {
		f = -f
	}
	// Shortest digits that round-trip, then lay them out by the ES rules.
	e := strconv.FormatFloat(f, 'e', -1, 64) // d.ddde±xx
	mant, expS, _ := strings.Cut(e, "e")
	exp, _ := strconv.Atoi(expS)
	digits := strings.Replace(mant, ".", "", 1)
	k := len(digits)
	nn := exp + 1 // decimal point position
	var s string
	switch {
	case k <= nn && nn <= 21:
		s = digits + strings.Repeat("0", nn-k)
	case 0 < nn && nn <= 21:
		s = digits[:nn] + "." + digits[nn:]
	case -6 < nn && nn <= 0:
		s = "0." + strings.Repeat("0", -nn) + digits
	default:
		sign := "+"
		if nn-1 < 0 {
			sign = "-"
		}
		ae := nn - 1
		if ae < 0 {
			ae = -ae
		}
		if k == 1 {
			s = digits + "e" + sign + strconv.Itoa(ae)
		} else {
			s = digits[:1] + "." + digits[1:] + "e" + sign + strconv.Itoa(ae)
		}
	}
	if neg {
		s = "-" + s
	}
	return s, nil
}
