package client

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
)

// strictDecode decodes one JSON document into v and refuses a duplicate key
// at any level, an unknown field, and trailing data. encoding/json alone takes
// the last of two equal keys without a word.
func strictDecode(raw []byte, v any) error {
	if err := noDuplicateKeys(raw); err != nil {
		return err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return errors.New("malformed document")
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return errors.New("trailing data")
	}
	return nil
}

func noDuplicateKeys(raw []byte) error {
	dec := json.NewDecoder(bytes.NewReader(raw))
	if err := walk(dec); err != nil {
		return err
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return errors.New("trailing data")
	}
	return nil
}

func walk(dec *json.Decoder) error {
	tok, err := dec.Token()
	if err != nil {
		return errors.New("malformed document")
	}
	d, ok := tok.(json.Delim)
	if !ok {
		return nil
	}
	switch d {
	case '{':
		seen := map[string]bool{}
		for dec.More() {
			k, err := dec.Token()
			if err != nil {
				return errors.New("malformed document")
			}
			key, _ := k.(string)
			if seen[key] {
				return errors.New("duplicate key")
			}
			seen[key] = true
			if err := walk(dec); err != nil {
				return err
			}
		}
	case '[':
		for dec.More() {
			if err := walk(dec); err != nil {
				return err
			}
		}
	}
	if _, err := dec.Token(); err != nil {
		return errors.New("malformed document")
	}
	return nil
}
