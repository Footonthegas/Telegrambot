package main

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

const (
	captchaTimeout = 10 * time.Second
)

func solveCaptchaGo(captchaBytes []byte) string {
	scriptPath := findCaptchaSolverScript()
	if scriptPath == "" {
		return ""
	}

	pythonBin := findPythonBinary()
	if pythonBin == "" {
		return ""
	}

	cmd := exec.Command(pythonBin, scriptPath)
	cmd.Stdin = bytes.NewReader(captchaBytes)

	var stdout bytes.Buffer
	var stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	done := make(chan error, 1)
	go func() {
		done <- cmd.Run()
	}()

	select {
	case <-time.After(captchaTimeout):
		_ = cmd.Process.Kill()
		return ""
	case err := <-done:
		if err != nil {
			return ""
		}
	}

	result := strings.TrimSpace(stdout.String())
	return result
}

func findCaptchaSolverScript() string {
	if p := os.Getenv("CAPTCHA_SOLVER_SCRIPT"); p != "" {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}

	candidates := []string{
		"solve_captcha_cli.py",
		"../solve_captcha_cli.py",
		"../../solve_captcha_cli.py",
	}

	if exe, err := os.Executable(); err == nil {
		exeDir := filepath.Dir(exe)
		candidates = append(candidates,
			filepath.Join(exeDir, "solve_captcha_cli.py"),
			filepath.Join(exeDir, "..", "solve_captcha_cli.py"),
		)
	}

	wd, err := os.Getwd()
	if err == nil {
		candidates = append(candidates,
			filepath.Join(wd, "solve_captcha_cli.py"),
			filepath.Join(wd, "..", "solve_captcha_cli.py"),
		)
	}

	for _, c := range candidates {
		abs, _ := filepath.Abs(c)
		if _, err := os.Stat(abs); err == nil {
			return abs
		}
	}

	return ""
}

func findPythonBinary() string {
	if venv := os.Getenv("VIRTUAL_ENV"); venv != "" {
		py := filepath.Join(venv, "Scripts", "python.exe")
		if runtime.GOOS == "linux" || runtime.GOOS == "darwin" {
			py = filepath.Join(venv, "bin", "python3")
		}
		if _, err := os.Stat(py); err == nil {
			return py
		}
	}

	scriptDir := ""
	if exe, err := os.Executable(); err == nil {
		scriptDir = filepath.Dir(exe)
	}
	wd, err := os.Getwd()
	if err == nil {
		for _, base := range []string{wd, scriptDir} {
			venvPath := filepath.Join(base, "..", ".venv")
			if _, err := os.Stat(venvPath); err == nil {
				py := filepath.Join(venvPath, "Scripts", "python.exe")
				if runtime.GOOS == "linux" || runtime.GOOS == "darwin" {
					py = filepath.Join(venvPath, "bin", "python3")
				}
				if _, err := os.Stat(py); err == nil {
					return py
				}
			}
		}
	}

	for _, name := range []string{"python3", "python"} {
		if p, err := exec.LookPath(name); err == nil && p != "" {
			return p
		}
	}
	return ""
}
