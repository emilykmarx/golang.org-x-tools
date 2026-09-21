package conftamer

import (
	"fmt"
	"go/types"

	"golang.org/x/tools/gopls/internal/golang"
)

/* Utilities for interacting with info from gopls */

// Whether type is "basic": not declared in package scope, or basic types
func BasicType(typ golang.TypeInfo) bool {
	_, basic_type := typ.TypeInfo.Type().(*types.Basic)
	if typ.TypeInfo.Parent() == nil || typ.TypeInfo.Parent().Parent() != types.Universe || basic_type {
		// e.g. function-local types, or `error`
		// Can cause TypeName to segfault - don't call it here
		return true
	}
	return false
}

// Populate methods the type implements
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

// If n is a struct, populate its field tags
func CopyTags(node *CTypeNode) {
	struct_info := IsStruct(node.TypeInfo)
	if struct_info == nil {
		// not a struct
		return
	}
	tags := make(map[string]string)

	for i := range struct_info.NumFields() {
		tags[struct_info.Field(i).Name()] = struct_info.Tag(i)
	}

	node.Tags = tags
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
