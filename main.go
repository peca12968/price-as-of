package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"unicode/utf8"
)

const usage = "usage: reach-sketch build PRECISION INPUT|- OUTPUT | merge OUTPUT INPUT... | report FILE"

func scanIDs(input io.Reader, s *sketch) error {
	scanner := bufio.NewScanner(input)
	scanner.Buffer(make([]byte, 4099), 4099)
	for line := 1; scanner.Scan(); line++ {
		id := scanner.Bytes()
		if len(id) == 0 || len(id) > 4096 || !utf8.Valid(id) {
			return fmt.Errorf("line %d: expected a UTF-8 ID of 1 to 4096 bytes", line)
		}
		s.add(id)
	}
	return scanner.Err()
}

func readSketch(path string) (*sketch, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, (1<<maxPrecision)+overhead+1))
	if err != nil {
		return nil, err
	}
	state, err := decode(data)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return state, nil
}

func publish(path string, s *sketch) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), ".reach-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	_, err = tmp.Write(s.encode())
	closeErr := tmp.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	return os.Link(tmp.Name(), path)
}

func run(args []string, stdin io.Reader, stdout io.Writer) error {
	if len(args) == 0 {
		return errors.New(usage)
	}
	switch args[0] {
	case "build":
		if len(args) != 4 {
			return errors.New(usage)
		}
		p, err := strconv.Atoi(args[1])
		if err != nil {
			return err
		}
		s, err := newSketch(p)
		if err != nil {
			return err
		}
		if args[2] != "-" {
			f, err := os.Open(args[2])
			if err != nil {
				return err
			}
			defer f.Close()
			stdin = f
		}
		if err := scanIDs(stdin, s); err != nil {
			return err
		}
		return publish(args[3], s)
	case "merge":
		if len(args) < 3 {
			return errors.New(usage)
		}
		combined, err := readSketch(args[2])
		if err != nil {
			return err
		}
		for _, path := range args[3:] {
			next, err := readSketch(path)
			if err != nil {
				return err
			}
			combined = merge(combined, next)
		}
		return publish(args[1], combined)
	case "report":
		if len(args) != 2 {
			return errors.New(usage)
		}
		s, err := readSketch(args[1])
		if err != nil {
			return err
		}
		return json.NewEncoder(stdout).Encode(map[string]any{
			"precision": s.p, "registers": len(s.registers),
			"estimated_distinct":              math.Round(s.estimate()),
			"typical_relative_standard_error": 1.04 / math.Sqrt(float64(len(s.registers))),
		})
	default:
		return errors.New(usage)
	}
}

func main() {
	if err := run(os.Args[1:], os.Stdin, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
