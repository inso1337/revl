package stc_test

// 可执行示例：与 README quick start 同源，go test 每次运行都会验证输出，
// 保证文档与行为不腐化。pkg.go.dev 会展示本文件中的 Example。

import (
	"context"
	"fmt"

	stc "github.com/0xdenny218/stc-go"
)

// 先装载的消费者因依赖未满足停在 Pending；提供者装载后，
// 依赖门控放行，消费者进入 Active 并读到服务值。
func Example() {
	greeting := stc.NewKey[string]("greeting")

	root := stc.New()
	defer root.Close()

	consumer := root.Load(stc.Component{
		Name:   "consumer",
		Inject: []stc.Key{greeting},
		Apply: func(c *stc.Context) (stc.Inverse, error) {
			msg, err := stc.Service[string](c, greeting)
			if err != nil {
				return nil, err
			}
			fmt.Println("consumer saw:", msg)
			return nil, nil
		},
	})

	root.Load(stc.Component{
		Name:    "provider",
		Provide: []stc.Key{greeting},
		Apply: func(c *stc.Context) (stc.Inverse, error) {
			_, err := c.Provide(greeting, "hello, spatiotemporal world")
			return nil, err
		},
	})

	if err := consumer.Ready(context.Background()); err != nil {
		panic(err)
	}

	// Output: consumer saw: hello, spatiotemporal world
}
