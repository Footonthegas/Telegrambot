package main

import (
	"bytes"
	"io"
	"net/http"
	"net/http/cookiejar"
	"os"
	"os/exec"
	"regexp"
	"strings"
	"testing"
)

func TestSolveCaptchaGo(t *testing.T) {
	captchaBytes := downloadTestCaptcha(t)
	if len(captchaBytes) == 0 {
		t.Skip("could not download test CAPTCHA")
	}

	result := solveCaptchaGo(captchaBytes)
	t.Logf("CAPTCHA solved: %q (len=%d)", result, len(result))

	if len(result) < 4 {
		t.Fatalf("expected at least 4 chars, got %q", result)
	}

	for _, c := range result {
		if c < '0' || c > '9' {
			t.Fatalf("expected only digits, got %q", result)
		}
	}
}

func downloadTestCaptcha(t *testing.T) []byte {
	base := "https://www.imsnsit.org/imsnsit/"
	jar, _ := cookiejar.New(nil)
	client := &http.Client{Jar: jar}

	get := func(path, referer string) string {
		req, _ := http.NewRequest("GET", base+path, nil)
		if referer != "" {
			req.Header.Set("Referer", referer)
		}
		req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/146.0.0.0 Safari/537.36")
		resp, err := client.Do(req)
		if err != nil {
			t.Logf("GET %s failed: %v", path, err)
			return ""
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(resp.Body)
		return string(body)
	}

	get("", "")
	get("plum5_fw_login.php?t=sw&w=1", base)
	get("student.htm", base)
	get("student_login110.php", base+"student.htm")
	loginHTML := get("student_login.php", base+"student.htm")

	capsrcRe := regexp.MustCompile(`<img src='([^']+captcha[^']+)' id='captchaimg'`)
	m := capsrcRe.FindStringSubmatch(loginHTML)
	if len(m) < 2 {
		return nil
	}

	req, _ := http.NewRequest("GET", base+m[1], nil)
	req.Header.Set("Referer", base+"student_login.php")
	req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/146.0.0.0 Safari/537.36")
	resp, err := client.Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	buf, _ := io.ReadAll(resp.Body)
	return buf
}

func TestFindCaptchaSolverScript(t *testing.T) {
	scriptPath := findCaptchaSolverScript()
	t.Logf("Found script at: %q", scriptPath)
	if scriptPath == "" {
		t.Fatal("script not found")
	}
	if _, err := os.Stat(scriptPath); err != nil {
		t.Fatalf("script does not exist: %v", err)
	}
}

func TestFindPythonBinary(t *testing.T) {
	py := findPythonBinary()
	t.Logf("Found Python at: %q", py)
	if py == "" {
		t.Fatal("Python not found")
	}
}

func TestSolveCaptchaGoWithStdin(t *testing.T) {
	captchaBytes := downloadTestCaptcha(t)
	if len(captchaBytes) == 0 {
		t.Skip("could not download test CAPTCHA")
	}

	result := solveCaptchaGo(captchaBytes)
	if len(result) < 4 {
		t.Fatalf("expected at least 4 chars, got %q", result)
	}
	t.Logf("Go subprocess CAPTCHA solve: %q", result)

	// Cross-check: solve the same image via direct Python call
	scriptPath := findCaptchaSolverScript()
	if scriptPath == "" {
		return
	}
	py := findPythonBinary()
	if py == "" {
		return
	}

	cmd := exec.Command(py, scriptPath)
	cmd.Stdin = bytes.NewReader(captchaBytes)
	var pyOut bytes.Buffer
	cmd.Stdout = &pyOut
	cmd.Stderr = io.Discard
	if err := cmd.Run(); err != nil {
		t.Fatalf("Python CLI failed: %v", err)
	}

	pyResult := strings.TrimSpace(pyOut.String())
	t.Logf("Direct Python solve: %q", pyResult)

	if result != pyResult {
		t.Errorf("Go result %q != Python result %q", result, pyResult)
	}
}
