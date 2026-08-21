package main

import (
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/PuerkitoBio/goquery"
)

func Itoa(n int) string {
	return strconv.Itoa(n)
}

type RegisteredCourse struct {
	Code    string `json:"code"`
	Name    string `json:"name"`
	Section string `json:"section"`
	Group   string `json:"group"`
	Batch   string `json:"batch"`
}

type TimetableSlot struct {
	Time    string `json:"time"`
	Subject string `json:"subject"`
	Room    string `json:"room,omitempty"`
}

func normalizeGroupLabel(raw string) string {
	lower := strings.ToLower(strings.TrimSpace(raw))
	if lower == "" {
		return ""
	}
	re := regexp.MustCompile(`grp\s*[- ]?\s*(\d+)`)
	m := re.FindStringSubmatch(lower)
	if len(m) >= 2 {
		return "grp-" + m[1]
	}
	return strings.TrimSpace(raw)
}

func extractRoom(raw string) string {
	patterns := []string{
		`(?i)Room\s*[:\-]?\s*([A-Z]?\d{2,})`,
		`(?i)Rm\s*[:\-]?\s*([A-Z]?\d{2,})`,
		`(?i)Room\s*[:\-]?\s*([A-Z]{1,2}\d{1,3})`,
	}
	for _, p := range patterns {
		m := regexp.MustCompile(p).FindStringSubmatch(raw)
		if len(m) >= 2 {
			return m[1]
		}
	}
	return ""
}

func parseRegisteredCourses(html string) []RegisteredCourse {
	if html == "" {
		return nil
	}

	doc, err := goquery.NewDocumentFromReader(strings.NewReader(html))
	if err != nil {
		return nil
	}

	var courses []RegisteredCourse

	doc.Find("table").Each(func(i int, table *goquery.Selection) {
		rows := table.Find("tr")
		if rows.Length() < 2 {
			return
		}

		headerRowIndex := -1
		var header []string

		for rowIdx := 0; rowIdx < rows.Length() && rowIdx < 6; rowIdx++ {
			cells := rows.Eq(rowIdx).Find("th, td")
			var texts []string
			cells.Each(func(j int, cell *goquery.Selection) {
				t := regexp.MustCompile(`\s+`).ReplaceAllString(strings.TrimSpace(cell.Text()), " ")
				texts = append(texts, strings.ToLower(t))
			})
			if len(texts) < 4 {
				continue
			}
			hasSubjectCode := false
			hasGroup := false
			for _, c := range texts {
				if strings.Contains(c, "subject") && strings.Contains(c, "code") {
					hasSubjectCode = true
				}
				if strings.Contains(c, "group") {
					hasGroup = true
				}
			}
			if hasSubjectCode && hasGroup {
				headerRowIndex = rowIdx
				header = texts
				break
			}
		}

		if headerRowIndex < 0 || len(header) < 4 {
			return
		}

		findCol := func(keys ...string) int {
			for i, h := range header {
				allMatch := true
				for _, k := range keys {
					if !strings.Contains(h, k) {
						allMatch = false
						break
					}
				}
				if allMatch {
					return i
				}
			}
			return -1
		}

		codeIdx := findCol("subject", "code")
		nameIdx := findCol("subject", "name")
		sectionIdx := findCol("section")
		groupIdx := findCol("group")
		if codeIdx < 0 || groupIdx < 0 {
			return
		}

		for rowIdx := headerRowIndex + 1; rowIdx < rows.Length(); rowIdx++ {
			cells := rows.Eq(rowIdx).Find("th, td")
			var texts []string
			cells.Each(func(k int, cell *goquery.Selection) {
				texts = append(texts, strings.TrimSpace(cell.Text()))
			})

			maxIdx := codeIdx
			if groupIdx > maxIdx {
				maxIdx = groupIdx
			}
			if sectionIdx >= 0 && sectionIdx > maxIdx {
				maxIdx = sectionIdx
			}
			if nameIdx >= 0 && nameIdx > maxIdx {
				maxIdx = nameIdx
			}
			if len(texts) <= maxIdx {
				continue
			}

			code := strings.ToUpper(strings.TrimSpace(texts[codeIdx]))
			if !regexp.MustCompile(`^[A-Z]{2,}[A-Z0-9]*\d{2,}$`).MatchString(code) {
				continue
			}

			name := ""
			if nameIdx >= 0 && nameIdx < len(texts) {
				name = strings.TrimSpace(texts[nameIdx])
			}
			sectionRaw := ""
			if sectionIdx >= 0 && sectionIdx < len(texts) {
				sectionRaw = strings.TrimSpace(texts[sectionIdx])
			}
			groupRaw := ""
			if groupIdx >= 0 && groupIdx < len(texts) {
				groupRaw = strings.TrimSpace(texts[groupIdx])
			}

		section := ""
		batch := ""
		secMatch := regexp.MustCompile(`(\d+)\s*/\s*(\d+)`).FindStringSubmatch(sectionRaw)
		if len(secMatch) >= 3 {
			section = secMatch[1]
			batch = secMatch[2]
		} else {
			numMatch := regexp.MustCompile(`\d+`).FindString(sectionRaw)
			section = numMatch
		}

			courses = append(courses, RegisteredCourse{
				Code:    code,
				Name:    name,
				Section: section,
				Group:   normalizeGroupLabel(groupRaw),
				Batch:   batch,
			})
		}
	})

	return courses
}

func findRegisteredCoursesLink(menuHTML string, base string) string {
	if menuHTML == "" {
		return ""
	}

	doc, err := goquery.NewDocumentFromReader(strings.NewReader(menuHTML))
	if err != nil {
		return ""
	}

	bestScore := 0
	bestURL := ""

	doc.Find("a").Each(func(i int, a *goquery.Selection) {
		href, _ := a.Attr("href")
		href = strings.TrimSpace(href)
		if href == "" {
			return
		}
		text := regexp.MustCompile(`\s+`).ReplaceAllString(strings.TrimSpace(a.Text()), " ")
		text = strings.ToLower(text)
		score := 0
		if strings.Contains(text, "current") && strings.Contains(text, "sem") &&
			strings.Contains(text, "course") && strings.Contains(text, "registered") {
			score += 8
		} else if strings.Contains(text, "registered courses") {
			score += 5
		} else if strings.Contains(text, "registered") {
			score += 2
		} else if strings.Contains(text, "courses") {
			score += 1
		}
		if score > 0 && score > bestScore {
			bestScore = score
			bestURL = href
		}
	})

	if bestURL == "" {
		re := regexp.MustCompile(`(?i)href=['"]([^'"]+)['"][^>]*>.*?current.*?sem.*?course.*?registered`)
		m := re.FindStringSubmatch(menuHTML)
		if len(m) >= 2 {
			bestURL = m[1]
		}
	}

	if bestURL == "" {
		re := regexp.MustCompile(`(?i)href=['"]([^'"]*(?:course|subject)[^'"]*)['"]`)
		m := re.FindStringSubmatch(menuHTML)
		if len(m) >= 2 {
			bestURL = m[1]
		}
	}

	if bestURL != "" && !strings.HasPrefix(bestURL, "http") {
		bestURL = base + bestURL
	}
	return bestURL
}

func findTimetableLink(menuHTML string, base string) string {
	if menuHTML == "" {
		return ""
	}

	doc, err := goquery.NewDocumentFromReader(strings.NewReader(menuHTML))
	if err != nil {
		return ""
	}

	bestScore := 0
	bestURL := ""

	doc.Find("a").Each(func(i int, a *goquery.Selection) {
		href, _ := a.Attr("href")
		href = strings.TrimSpace(href)
		if href == "" {
			return
		}
		text := regexp.MustCompile(`\s+`).ReplaceAllString(strings.TrimSpace(a.Text()), " ")
		text = strings.ToLower(text)
		hrefLower := strings.ToLower(href)

		score := 0
		if strings.Contains(text, "time table") || strings.Contains(text, "timetable") {
			score += 6
		}
		if strings.Contains(text, "my") {
			score += 1
		}
		if strings.Contains(text, "class schedule") || strings.Contains(text, "schedule") {
			score += 4
		}
		if strings.Contains(hrefLower, "time") && strings.Contains(hrefLower, "table") {
			score += 4
		}
		if strings.Contains(hrefLower, "timetable") {
			score += 3
		}
		if strings.Contains(hrefLower, "schedule") {
			score += 2
		}
		if strings.Contains(hrefLower, "plum_url.php") {
			score += 1
		}

		if score > 0 && score > bestScore {
			bestScore = score
			bestURL = href
		}
	})

	if bestURL == "" {
		re := regexp.MustCompile(`(?i)href=['"]([^'"]+)['"][^>]*>\s*(?:my\s*)?(?:time\s*table|timetable|class\s*schedule|schedule)\s*<`)
		m := re.FindStringSubmatch(menuHTML)
		if len(m) >= 2 {
			bestURL = m[1]
		}
	}

	if bestURL == "" {
		re := regexp.MustCompile(`(?i)href=['"]([^'"]*(?:timetable|time_table|schedule)[^'"]*)['"]`)
		m := re.FindStringSubmatch(menuHTML)
		if len(m) >= 2 {
			bestURL = m[1]
		}
	}

	if bestURL != "" && !strings.HasPrefix(bestURL, "http") {
		bestURL = base + bestURL
	}
	return bestURL
}

func looksLikeTimeLabel(text string) bool {
	t := strings.ToLower(strings.TrimSpace(text))
	if t == "" {
		return false
	}
	if regexp.MustCompile(`\b\d{1,2}(:\d{2})?\s*(am|pm)?\s*[-–to]+\s*\d{1,2}(:\d{2})?\s*(am|pm)?\b`).MatchString(t) {
		return true
	}
	if regexp.MustCompile(`\bperiod\s*\d+\b`).MatchString(t) {
		return true
	}
	return false
}

func to12h(hour int, minute int) (int, string) {
	suffix := "am"
	if hour >= 12 {
		suffix = "pm"
	}
	h12 := hour % 12
	if h12 == 0 {
		h12 = 12
	}
	return h12, suffix
}

func formatSlotTime(raw string) string {
	text := strings.TrimSpace(raw)
	if text == "" {
		return ""
	}

	re := regexp.MustCompile(`(?i)(?P<h1>\d{1,2})(?::(?P<m1>\d{2}))?\s*(?P<a1>am|pm)?\s*[-–to]+\s*(?P<h2>\d{1,2})(?::(?P<m2>\d{2}))?\s*(?P<a2>am|pm)?`)
	m := re.FindStringSubmatch(text)
	if m == nil {
		return text
	}

	h1 := atoi(m[1])
	h2 := atoi(m[4])
	m1 := 0
	m2 := 0
	if m[2] != "" {
		m1 = atoi(m[2])
	}
	if m[5] != "" {
		m2 = atoi(m[5])
	}
	a1 := strings.ToLower(m[3])
	a2 := strings.ToLower(m[6])

	if a1 == "" && a2 == "" {
		if h2 == 12 && m2 > 0 && h1 >= 2 && h1 <= 11 {
			h2 = h1 + 1
			m2 = 0
		}
		if h1 >= 1 && h1 <= 11 && h2 >= 1 && h2 <= 11 {
			if h2 <= h1 {
				h2 += 12
			}
			if h1 <= 6 {
				h1 += 12
				if h2 <= 6 {
					h2 += 12
				}
			}
		}
		if h1 == 12 && h2 < 12 && h2 >= 1 {
			h2 += 12
		}
		sh, ss := to12h(h1, m1)
		eh, es := to12h(h2, m2)
		if ss == es && m1 == 0 && m2 == 0 {
			return Itoa(sh) + "-" + Itoa(eh) + es
		}
		if ss == es {
			return Itoa(sh) + ":" + padZero(m1) + "-" + Itoa(eh) + ":" + padZero(m2) + es
		}
		return Itoa(sh) + ":" + padZero(m1) + ss + "-" + Itoa(eh) + ":" + padZero(m2) + es
	}

	to24 := func(h int, mer string) int {
		hh := h % 12
		if mer == "pm" {
			hh += 12
		}
		return hh
	}

	endSuffix := a2
	if endSuffix == "" {
		endSuffix = a1
	}
	if endSuffix == "" {
		endSuffix = "am"
	}
	startSuffix := a1
	if startSuffix == "" {
		startSuffix = endSuffix
	}

	startH24 := to24(h1, startSuffix)
	endH24 := to24(h2, endSuffix)
	if a2 == "" && endH24 <= startH24 {
		endH24 += 12
	}
	if a1 == "" && startH24 > endH24 {
		startH24 = startH24 - 12
		if startH24 < 0 {
			startH24 = 0
		}
	}

	sh, ss := to12h(startH24, m1)
	eh, es := to12h(endH24, m2)

	if ss == es && m1 == 0 && m2 == 0 {
		return Itoa(sh) + "-" + Itoa(eh) + es
	}
	if ss == es {
		return Itoa(sh) + ":" + padZero(m1) + "-" + Itoa(eh) + ":" + padZero(m2) + es
	}
	return Itoa(sh) + ":" + padZero(m1) + ss + "-" + Itoa(eh) + ":" + padZero(m2) + es
}

func isTodayLabel(label string) bool {
	clean := regexp.MustCompile(`[^a-z]`).ReplaceAllString(strings.ToLower(strings.TrimSpace(label)), "")
	if clean == "" {
		return false
	}
	today := todayTokens()
	for token := range today {
		if strings.HasPrefix(clean, token) {
			return true
		}
	}
	return false
}

func todayTokens() map[string]bool {
	aliases := map[string][]string{
		"monday":    {"mon", "monday"},
		"tuesday":   {"tue", "tues", "tuesday"},
		"wednesday": {"wed", "wednesday"},
		"thursday":  {"thu", "thur", "thurs", "thursday"},
		"friday":    {"fri", "friday"},
		"saturday":  {"sat", "saturday"},
		"sunday":    {"sun", "sunday"},
	}

	result := make(map[string]bool)

	now := time.Now()
	full := strings.ToLower(now.Weekday().String())
	todayAliases, ok := aliases[full]
	if ok {
		for _, a := range todayAliases {
			result[a] = true
		}
	}
	result[full] = true
	short := full[:3]
	result[short] = true

	return result
}

func normalizeSubjectFromSlot(raw string) string {
	text := regexp.MustCompile(`\s+`).ReplaceAllString(strings.TrimSpace(raw), " ")
	if text == "" {
		return ""
	}

	isLab := regexp.MustCompile(`(?i)\bgrp\s*[- ]?\s*[12]\b`).MatchString(text)
	text = regexp.MustCompile(`(?i)\bgrp\s*[- ]?\s*[12]\b`).ReplaceAllString(text, "")

	codeMatch := regexp.MustCompile(`\b([A-Z]{2,}[A-Z0-9]*\d{2,})\b`).FindString(text)
	subjectCode := strings.ToUpper(codeMatch)
	if subjectCode != "" {
		text = regexp.MustCompile(`\b`+regexp.QuoteMeta(subjectCode)+`\b`).ReplaceAllString(text, "")
	}

	text = regexp.MustCompile(`^[\s:\-–|/]+`).ReplaceAllString(text, "")
	text = regexp.MustCompile(`\([^)]*\)`).ReplaceAllString(text, "")

	text = regexp.MustCompile(`(?i)Sem\s*[:\-]?\s*\d+`).ReplaceAllString(text, "")
	text = regexp.MustCompile(`(?i)Bat\s*[:\-]?\s*\d+`).ReplaceAllString(text, "")
	text = regexp.MustCompile(`(?i)Room\s*[:\-]?\s*\d+`).ReplaceAllString(text, "")
	text = regexp.MustCompile(`(?i)Sec\s*[:\-]?\s*\d+`).ReplaceAllString(text, "")
	text = regexp.MustCompile(`(?i)Grp\s*[- ]?\s*\d+`).ReplaceAllString(text, "")

	text = regexp.MustCompile(`^[A-Z]{2,}[A-Z0-9]*\d{2,}\s*[-:]+\s*`).ReplaceAllString(text, "")
	text = regexp.MustCompile(`\s{2,}`).ReplaceAllString(text, " ")
	text = strings.Trim(text, " -:;,|")

	if text == "" && subjectCode != "" {
		text = subjectCode
	}

	if isLab && text != "" && !strings.HasSuffix(strings.ToLower(text), "lab") {
		text = text + " Lab"
	}

	return strings.TrimSpace(text)
}

func pickSubjectForSlot(rawCell string, registered []RegisteredCourse) string {
	raw := regexp.MustCompile(`\s+`).ReplaceAllString(strings.TrimSpace(rawCell), " ")
	if raw == "" {
		return ""
	}
	lower := strings.ToLower(raw)
	empty := map[string]bool{"-": true, "--": true, "na": true, "n/a": true, "off": true, "holiday": true, "break": true, "lunch": true, "free": true}
	if empty[lower] {
		return ""
	}

	compressed := regexp.MustCompile(`(?i)(?:sem\s*[:\-]?\s*\d+.*?bat\s*[:\-]?\s*\d+)`).ReplaceAllString(raw, " ")
	compressed = regexp.MustCompile(`\s{2,}`).ReplaceAllString(compressed, " ")
	compressed = strings.TrimSpace(compressed)

	rawLower := strings.ToLower(raw)
	for _, c := range registered {
		if c.Name != "" && rawLower == strings.ToLower(c.Name) {
			return c.Name
		}
		if c.Name != "" && strings.Contains(rawLower, strings.ToLower(c.Name)) {
			return c.Name
		}
	}

	searchText := compressed
	codeMatches := regexp.MustCompile(`\b([A-Z]{2,}[A-Z0-9]*\d{2,})\b`).FindAllString(searchText, -1)
	if len(codeMatches) == 0 {
		codeMatches = regexp.MustCompile(`([A-Z]{2,}[A-Z0-9]*\d{2,})`).FindAllString(searchText, -1)
	}
	regMap := make(map[string]RegisteredCourse)
	for _, c := range registered {
		regMap[c.Code] = c
	}
	var uniqueCodes []string
	seen := make(map[string]bool)
	for _, c := range codeMatches {
		cu := strings.ToUpper(c)
		if regMap[cu] != (RegisteredCourse{}) && !seen[cu] {
			uniqueCodes = append(uniqueCodes, cu)
			seen[cu] = true
		}
	}

	if len(uniqueCodes) == 0 {
		for _, c := range registered {
			if c.Name != "" && strings.Contains(rawLower, strings.ToLower(c.Name)) {
				return c.Name
			}
		}
		return normalizeSubjectFromSlot(raw)
	}

	type matchResult struct {
		code     string
		name     string
		isLab    bool
		disq     bool
	}
	var results []matchResult

	for _, code := range uniqueCodes {
		reg := regMap[code]
		slotText := raw
		disq := false
		isLab := false
		matched := false

		allBatchHits := regexp.MustCompile(`(?i)`+regexp.QuoteMeta(code)+`[^\n]*?bat\s*[:\- ]\s*(\d+)`).FindAllStringSubmatch(slotText, -1)
		allGroupHits := regexp.MustCompile(`(?i)`+regexp.QuoteMeta(code)+`[^\n]*?grp\s*[- ]?\s*(\d+)`).FindAllStringSubmatch(slotText, -1)
		secHit := regexp.MustCompile(`(?i)`+regexp.QuoteMeta(code)+`[^\n]*?sec\s*[:\- ]\s*(\d+)`).FindStringSubmatch(slotText)

		hasAnyBatchMarker := len(allBatchHits) > 0
		hasAnyGroupMarker := len(allGroupHits) > 0
		hasAnySectionMarker := len(secHit) >= 2

		if reg.Batch != "" {
			if hasAnyBatchMarker {
				found := false
				for _, bh := range allBatchHits {
					if len(bh) >= 2 && strings.TrimSpace(bh[1]) == reg.Batch {
						found = true
						break
					}
				}
				if found {
					matched = true
				} else {
					disq = true
				}
			}
		} else if hasAnyBatchMarker {
			disq = true
		}

		if !disq && reg.Group != "" {
			grpNum := strings.Split(reg.Group, "-")
			last := grpNum[len(grpNum)-1]
			if hasAnyGroupMarker {
				found := false
				for _, gh := range allGroupHits {
					if len(gh) >= 2 && strings.TrimSpace(gh[1]) == last {
						found = true
						break
					}
				}
				if found {
					matched = true
					if regexp.MustCompile(`(?i)grp\s*[- ]?\s*\d+`).MatchString(slotText) {
						isLab = true
					}
				} else {
					disq = true
				}
			}
		} else if !disq && hasAnyGroupMarker {
			disq = true
		}

		if !disq && !matched && reg.Section != "" {
			if hasAnyBatchMarker || hasAnyGroupMarker {
				disq = true
			} else if hasAnySectionMarker {
				if strings.TrimSpace(secHit[1]) != reg.Section {
					disq = true
				} else {
					matched = true
				}
			}
		}

		if !disq && !matched {
			if hasAnyBatchMarker || hasAnyGroupMarker || hasAnySectionMarker {
				disq = true
			} else {
				matched = true
			}
		}

		name := reg.Name
		if name == "" {
			name = code
		}

		results = append(results, matchResult{
			code:  code,
			name:  name,
			isLab: isLab,
			disq:  disq,
		})
	}

	var matchedResults []matchResult
	for _, r := range results {
		if !r.disq {
			matchedResults = append(matchedResults, r)
		}
	}

	if len(matchedResults) == 0 {
		for _, c := range registered {
			if c.Name != "" && strings.Contains(rawLower, strings.ToLower(c.Name)) {
				return c.Name
			}
		}

		hasBatch := regexp.MustCompile(`(?i)bat\s*[:\- ]\s*\d+`).MatchString(raw)
		hasGroup := regexp.MustCompile(`(?i)grp\s*[- ]?\s*\d+`).MatchString(raw)
		hasSection := regexp.MustCompile(`(?i)sec\s*[:\- ]\s*\d+`).MatchString(raw)

		if hasBatch || hasGroup || hasSection {
			userHasMarker := false
			for _, c := range registered {
				if c.Batch != "" || c.Group != "" || c.Section != "" {
					userHasMarker = true
					break
				}
			}
			if userHasMarker {
				return ""
			}
		}

		fallback := normalizeSubjectFromSlot(raw)
		if fallback != "" {
			return fallback
		}
		return raw
	}

	if len(matchedResults) == 1 {
		r := matchedResults[0]
		if r.isLab {
			return r.name + " Lab"
		}
		return r.name
	}

	for _, r := range matchedResults {
		if r.isLab {
			return r.name + " Lab"
		}
	}

	first := matchedResults[0]
	return first.name
}

func parseTimetable(html string, todayOnly bool, registered []RegisteredCourse) []TimetableSlot {
	if html == "" {
		return nil
	}

	doc, err := goquery.NewDocumentFromReader(strings.NewReader(html))
	if err != nil {
		return nil
	}

	var allSlots []TimetableSlot

	doc.Find("table").Each(func(i int, table *goquery.Selection) {
		rows := table.Find("tr")
		if rows.Length() < 2 {
			return
		}

		slots := tryParseMatrixTable(rows, todayOnly, registered)
		if len(slots) > 0 {
			allSlots = append(allSlots, slots...)
			return
		}

		slots = tryParseRowwiseTable(rows, todayOnly, registered)
		if len(slots) > 0 {
			allSlots = append(allSlots, slots...)
		}
	})

	return allSlots
}

func tryParseMatrixTable(rows *goquery.Selection, todayOnly bool, registered []RegisteredCourse) []TimetableSlot {
	headerIndex := -1
	var slotTimes []string

	for rowIdx := 0; rowIdx < rows.Length() && rowIdx < 8; rowIdx++ {
		cells := rows.Eq(rowIdx).Find("th, td")
		var texts []string
		cells.Each(func(j int, cell *goquery.Selection) {
			texts = append(texts, strings.TrimSpace(cell.Text()))
		})
		if len(texts) < 2 {
			continue
		}
		timeCount := 0
		var times []string
		for j, t := range texts {
			if j == 0 {
				times = append(times, "")
				continue
			}
			if looksLikeTimeLabel(t) {
				times = append(times, formatSlotTime(t))
				timeCount++
			} else {
				times = append(times, "")
			}
		}
		if timeCount >= 2 {
			headerIndex = rowIdx
			slotTimes = times
			break
		}
	}

	if headerIndex < 0 || len(slotTimes) == 0 {
		return nil
	}

	for rowIdx := headerIndex + 1; rowIdx < rows.Length(); rowIdx++ {
		cells := rows.Eq(rowIdx).Find("th, td")
		var texts []string
		cells.Each(func(k int, cell *goquery.Selection) {
			texts = append(texts, strings.TrimSpace(cell.Text()))
		})
		if len(texts) == 0 {
			continue
		}
		dayLabel := texts[0]
		if todayOnly && !isTodayLabel(dayLabel) {
			continue
		}
		var slots []TimetableSlot
		limit := len(texts)
		if len(slotTimes) < limit {
			limit = len(slotTimes)
		}
		for colIdx := 1; colIdx < limit; colIdx++ {
			timeLabel := slotTimes[colIdx]
			if timeLabel == "" {
				continue
			}
			rawSubject := texts[colIdx]
			if rawSubject == "" {
				continue
			}
			lower := strings.ToLower(rawSubject)
			empty := map[string]bool{"-": true, "--": true, "na": true, "n/a": true, "off": true, "holiday": true, "break": true, "lunch": true, "free": true}
			if empty[lower] {
				continue
			}
			subject := pickSubjectForSlot(rawSubject, registered)
			if subject == "" {
				continue
			}
			room := extractRoom(rawSubject)
			slots = append(slots, TimetableSlot{Time: timeLabel, Subject: subject, Room: room})
		}
		if todayOnly && len(slots) > 0 {
			return slots
		}
		if len(slots) > 0 {
			return slots
		}
	}
	return nil
}

func tryParseRowwiseTable(rows *goquery.Selection, todayOnly bool, registered []RegisteredCourse) []TimetableSlot {
	cells0 := rows.Eq(0).Find("th, td")
	var headers []string
	cells0.Each(func(j int, cell *goquery.Selection) {
		headers = append(headers, strings.ToLower(strings.TrimSpace(cell.Text())))
	})
	if len(headers) < 3 {
		return nil
	}

	dayIdx := -1
	timeIdx := -1
	subjIdx := -1
	for i, h := range headers {
		if dayIdx < 0 && (strings.Contains(h, "day") || strings.Contains(h, "weekday")) {
			dayIdx = i
		}
		if timeIdx < 0 && (strings.Contains(h, "time") || strings.Contains(h, "slot") || strings.Contains(h, "period")) {
			timeIdx = i
		}
		if subjIdx < 0 && (strings.Contains(h, "subject") || strings.Contains(h, "course") || strings.Contains(h, "paper")) {
			subjIdx = i
		}
	}

	if timeIdx < 0 || subjIdx < 0 {
		return nil
	}

	var slots []TimetableSlot
	for rowIdx := 1; rowIdx < rows.Length(); rowIdx++ {
		cells := rows.Eq(rowIdx).Find("th, td")
		var texts []string
		cells.Each(func(k int, cell *goquery.Selection) {
			texts = append(texts, strings.TrimSpace(cell.Text()))
		})
		maxIdx := timeIdx
		if subjIdx > maxIdx {
			maxIdx = subjIdx
		}
		if dayIdx >= 0 && dayIdx > maxIdx {
			maxIdx = dayIdx
		}
		if len(texts) <= maxIdx {
			continue
		}
		if dayIdx >= 0 && !isTodayLabel(texts[dayIdx]) {
			continue
		}
		timeLabel := formatSlotTime(texts[timeIdx])
		if timeLabel == "" {
			continue
		}
		subject := pickSubjectForSlot(texts[subjIdx], registered)
		if subject == "" {
			continue
		}
		room := extractRoom(texts[subjIdx])
		slots = append(slots, TimetableSlot{Time: timeLabel, Subject: subject, Room: room})
	}
	return slots
}

func parseTimetableWeek(html string, registered []RegisteredCourse) map[string][]TimetableSlot {
	if html == "" {
		return nil
	}

	doc, err := goquery.NewDocumentFromReader(strings.NewReader(html))
	if err != nil {
		return nil
	}

	result := make(map[string][]TimetableSlot)

	doc.Find("table").Each(func(i int, table *goquery.Selection) {
		rows := table.Find("tr")
		if rows.Length() < 2 {
			return
		}

		headerIndex := -1
		var slotTimes []string

		for rowIdx := 0; rowIdx < rows.Length() && rowIdx < 8; rowIdx++ {
			cells := rows.Eq(rowIdx).Find("th, td")
			var texts []string
			cells.Each(func(j int, cell *goquery.Selection) {
				texts = append(texts, strings.TrimSpace(cell.Text()))
			})
			if len(texts) < 2 {
				continue
			}
			timeCount := 0
			var times []string
			for j, t := range texts {
				if j == 0 {
					times = append(times, "")
					continue
				}
				if looksLikeTimeLabel(t) {
					times = append(times, formatSlotTime(t))
					timeCount++
				} else {
					times = append(times, "")
				}
			}
		if timeCount >= 2 {
			headerIndex = rowIdx
			slotTimes = times
			break
		}
		}

		if headerIndex < 0 || len(slotTimes) == 0 {
			return
		}

		for rowIdx := headerIndex + 1; rowIdx < rows.Length(); rowIdx++ {
			cells := rows.Eq(rowIdx).Find("th, td")
			var texts []string
			cells.Each(func(k int, cell *goquery.Selection) {
				texts = append(texts, strings.TrimSpace(cell.Text()))
			})
			if len(texts) == 0 {
				continue
			}
			dayLabel := strings.TrimSpace(texts[0])
			dayLower := strings.ToLower(dayLabel)
			if dayLower == "overall" || dayLower == "total" || dayLower == "legend" || dayLower == "note" {
				continue
			}

			var daySlots []TimetableSlot
			limit := len(texts)
			if len(slotTimes) < limit {
				limit = len(slotTimes)
			}
			for colIdx := 1; colIdx < limit; colIdx++ {
				timeLabel := slotTimes[colIdx]
				if timeLabel == "" {
					continue
				}
				rawSubject := texts[colIdx]
				if rawSubject == "" {
					continue
				}
				lower := strings.ToLower(rawSubject)
				empty := map[string]bool{"-": true, "--": true, "na": true, "n/a": true, "off": true, "holiday": true, "break": true, "lunch": true, "free": true}
				if empty[lower] {
					continue
				}
				subject := pickSubjectForSlot(rawSubject, registered)
				if subject == "" {
					continue
				}
				room := extractRoom(rawSubject)
				daySlots = append(daySlots, TimetableSlot{Time: timeLabel, Subject: subject, Room: room})
			}
			if len(daySlots) > 0 {
				result[dayLabel] = daySlots
			}
		}
	})

	return result
}

func padZero(n int) string {
	if n < 10 {
		return "0" + Itoa(n)
	}
	return Itoa(n)
}

func atoi(s string) int {
	n := 0
	for _, c := range s {
		if c >= '0' && c <= '9' {
			n = n*10 + int(c-'0')
		}
	}
	return n
}
