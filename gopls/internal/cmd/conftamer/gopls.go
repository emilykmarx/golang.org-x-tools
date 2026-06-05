package conftamer

import (
	"fmt"
	"go/types"
	"slices"
	"strings"
)

/* Utilities for interacting with info from gopls */

// Methods the type implements
func CopyMethods(node *CTypeNode) {
	methods := []FullTypeName{}
	var method_typ *types.Named
	switch typ := node.TypeInfo.(type) {
	case *types.Named:
		method_typ = typ
	case *types.Alias:
		ok := false
		method_typ, ok = types.Unalias(typ).(*types.Named)
		if !ok {
			// e.g. type X = struct{} - can't define methods on it (but can on type X = Y)
		}
	default:
		panic(fmt.Sprintf("Try to get methods for unsupported type %v", CTypeNodeHash(*node)))
	}

	if method_typ != nil {
		for method := range method_typ.Methods() {
			methods = append(methods, FullTypeName(method.FullName()))
		}
	}
	node.Methods = methods
}

func TypeNameSafe(type_info *types.TypeName) FullTypeName {
	pkg := "<nil>"
	if type_info.Pkg() != nil {
		pkg = type_info.Pkg().Path()
	}
	return FullTypeName(pkg + "." + type_info.Name())
}
func TypeName(type_info *types.TypeName) FullTypeName {
	return FullTypeName(type_info.Pkg().Path() + "." + type_info.Name())
}

func IsStruct(type_info types.Type) *types.Struct {
	if struct_info, ok := type_info.Underlying().(*types.Struct); ok {
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
