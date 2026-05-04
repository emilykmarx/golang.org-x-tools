package conftamer

import (
	"go/types"
	"slices"
	"strings"
)

/* Utilities for interacting with info from gopls */

func typeName(type_info *types.TypeName) string {
	return type_info.Pkg().Path() + "." + type_info.Name()
}

func IsStruct(type_info *types.TypeName) *types.Struct {
	if struct_info, ok := type_info.Type().Underlying().(*types.Struct); ok {
		return struct_info
	}
	return nil
}

// Param key corresponding to struct field (tag key if tagged, else lowercase field name)
func FieldToParamKey(field *types.Var, tag string) string {
	param_key := ""

	if tag != "" {
		// Get tag key
		tag = strings.Split(tag, ":")[1]
		tag = strings.Trim(tag, "\"")
		tag_parts := strings.Split(tag, ",")
		param_key = tag_parts[0]
		tag_flags := tag_parts[1:]
		if slices.Contains(tag_flags, "inline") {
			// Will need this later (for getting full param name)
		}
	} else {
		// No tag => take key as lowercased field name:
		// Field could either be a key in the raw content (iff field name is uppercase, and lowercased version is in raw content),
		// or copied/otherwise derived from the raw content after unmarshaling
		param_key = strings.ToLower(field.Name())
	}
	return param_key
}
