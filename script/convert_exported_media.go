package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/sjzar/chatlog/pkg/util/dat2img"
)

func main() {
	mediaDir := flag.String("media-dir", "", "exported media dir")
	mediaRoot := flag.String("media-root", "", "wechat account root dir")
	imgKey := flag.String("img-key", "", "wechat image key")
	flag.Parse()

	if *mediaDir == "" {
		fmt.Fprintln(os.Stderr, "--media-dir is required")
		os.Exit(2)
	}

	if *imgKey != "" {
		dat2img.SetAesKey(*imgKey)
	}
	if *mediaRoot != "" {
		_, _ = dat2img.ScanAndSetXorKey(*mediaRoot)
	}

	converted := 0
	skipped := 0
	failed := 0

	err := filepath.Walk(*mediaDir, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			return nil
		}
		if strings.ToLower(filepath.Ext(path)) != ".dat" {
			return nil
		}

		data, err := os.ReadFile(path)
		if err != nil {
			failed++
			fmt.Fprintf(os.Stderr, "read failed: %s: %v\n", path, err)
			return nil
		}

		out, ext, err := dat2img.Dat2Image(data)
		if err != nil {
			skipped++
			return nil
		}

		target := strings.TrimSuffix(path, filepath.Ext(path)) + "." + ext
		if err := os.WriteFile(target, out, 0644); err != nil {
			failed++
			fmt.Fprintf(os.Stderr, "write failed: %s: %v\n", target, err)
			return nil
		}
		converted++
		return nil
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	fmt.Printf("converted=%d skipped=%d failed=%d\n", converted, skipped, failed)
}
